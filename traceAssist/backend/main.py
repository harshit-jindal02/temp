import os
import shutil
import zipfile
import uuid
import subprocess
import logging
from urllib.parse import urlparse, urlunparse, quote

from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator
from typing import Optional, Any

from git import Repo, GitCommandError
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader
from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError

# OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# New imports for alert handling
import httpx
from atlassian import Jira

# Load environment
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PAT_TOKEN = os.getenv("PAT_TOKEN")

# SigNoz & Loki & Jira envs
SIGNOZ_CLOUD_ENDPOINT = os.getenv("SIGNOZ_CLOUD_ENDPOINT")
SIGNOZ_CLOUD_API_KEY = os.getenv("SIGNOZ_CLOUD_API_KEY")

LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "").rstrip('/')
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_USER_EMAIL = os.getenv("JIRA_USER_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not OPENAI_API_KEY and not openai_client:
    logging.warning("OPENAI_API_KEY not found. AI suggestions will be unavailable.")

# Initialize Jira client
if JIRA_BASE_URL and JIRA_USER_EMAIL and JIRA_API_TOKEN:
    jira = Jira(
        url=JIRA_BASE_URL,
        username=JIRA_USER_EMAIL,
        password=JIRA_API_TOKEN
    )
    logging.info("Jira client configured.")
else:
    jira = None
    logging.warning("Jira environment variables missing; /alert endpoint will not work.")

# OpenTelemetry setup
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", SIGNOZ_CLOUD_ENDPOINT)
SERVICE_NAME = "traceassist-backend"

if OTEL_ENDPOINT:
    logging.info(f"OpenTelemetry configured with endpoint: {OTEL_ENDPOINT}")
    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
else:
    logging.warning("OTEL_EXPORTER_OTLP_ENDPOINT not found. OpenTelemetry tracing will be disabled.")
    provider = None

# FastAPI setup
app = FastAPI()

if provider:
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
else:
    logging.info("Skipping FastAPI OpenTelemetry instrumentation as provider is not configured.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Directories
BASE_DIR = "user-apps"
TEMPLATE_DIR = "templates"
K8S_OUTPUT_DIR = "k8s-generated"

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMPLATE_DIR, exist_ok=True)
os.makedirs(K8S_OUTPUT_DIR, exist_ok=True)


# --- Pydantic Models ---

class GitCloneRequest(BaseModel):
    repo_url: str = Field(..., description="The HTTPS URL of the repository (e.g., https://github.com/user/repo.git)")
    branch: Optional[str] = Field(default="main", description="The branch to clone. Defaults to 'main'. 'master' is normalized to 'main'.")

    @validator("branch", pre=True, always=True)
    @classmethod
    def normalize_default_branch(cls, v: Any) -> str:
        if v is None:
            return "main"
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return "main"
            if v == "master":
                return "main"
            return v
        raise TypeError(f"Branch must be a string or None; got {type(v).__name__}")

    @validator("repo_url")
    @classmethod
    def validate_repo_url_is_https(cls, v: str) -> str:
        if not v:
            raise ValueError("repo_url cannot be empty.")
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("repo_url must be an HTTP or HTTPS URL.")
        return v


class InstrumentRequest(BaseModel):
    app_id: str


class AISuggestionResponse(BaseModel):
    app_id: str
    suggestions: str
    model_used: Optional[str] = None


# --- Helper Functions ---

def detect_language(app_path: str) -> str:
    has_package_json = False
    py_count = 0
    java_count = 0

    if not os.path.isdir(app_path):
        logger.warning(f"Path provided to detect_language is not a directory: {app_path}")
        return "unknown"

    excluded_dirs = ['.git', 'node_modules', '__pycache__', 'venv', 'env', '.venv',
                     'target', 'build', 'dist', '.vscode', '.idea', 'docs', 'tests', 'test']

    for root, dirs, files in os.walk(app_path):
        dirs[:] = [d for d in dirs if d not in excluded_dirs]
        if "package.json" in files:
            has_package_json = True
        for f_name in files:
            if f_name.endswith(".py"):
                py_count += 1
            if f_name.endswith(".java"):
                java_count += 1

    if has_package_json:
        return "nodejs"
    if py_count > 0 and java_count == 0:
        return "python"
    if java_count > 0:
        return "java"
    if py_count > 0:
        return "python"

    logger.info(
        f"Language detection for {app_path}: Node.js (package.json): {has_package_json}, Python files: {py_count}, Java files: {java_count}. Result: unknown"
    )
    return "unknown"


PORT_MAP = {"nodejs": 3000, "python": 8000, "java": 8080, "unknown": 8080}


def generate_dockerfile(app_path: str, language: str, app_id: str) -> Optional[str]:
    dockerfile_content = None
    port = PORT_MAP.get(language, 8080)

    if language == "nodejs":
        dockerfile_content = f"""
FROM node:18-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev

FROM node:18-alpine
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY . .
EXPOSE {port}
CMD [ "npm", "start" ]
"""
    elif language == "python":
        dockerfile_content = f"""
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {port}
CMD [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}" ]
"""
    elif language == "java":
        dockerfile_content = f"""
FROM maven:3.8-openjdk-17 AS build
WORKDIR /app
COPY pom.xml .
COPY src ./src
RUN mvn package -DskipTests

FROM openjdk:17-jre-slim
WORKDIR /app
ARG JAR_FILE=/app/target/*.jar
COPY --from=build ${{JAR_FILE}} app.jar
EXPOSE {port}
ENTRYPOINT ["java","-jar","/app/app.jar"]
"""
    else:
        logger.error(f"Unsupported language '{language}' for Dockerfile generation for app {app_id}.")
        return None

    dockerfile_path = os.path.join(app_path, "Dockerfile")
    try:
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content.strip())
        logger.info(f"Dockerfile generated for app {app_id} ({language}) at {dockerfile_path}")
        return dockerfile_path
    except IOError as e:
        logger.error(f"Failed to write Dockerfile for app {app_id} at {dockerfile_path}: {e}", exc_info=True)
        return None


def build_user_image(app_id: str, app_source_dir: str, language: str) -> str:
    image_name = f"user-app-{app_id.lower()}:latest"
    logger.info(f"Attempting to generate Dockerfile for app {app_id} ({language}) in {app_source_dir}")
    dockerfile_path = generate_dockerfile(app_source_dir, language, app_id)  # type: ignore
    if not dockerfile_path or not os.path.exists(dockerfile_path):
        logger.error(f"Dockerfile generation failed or Dockerfile not found for app {app_id}.")
        raise HTTPException(status_code=500, detail=f"Dockerfile generation failed for language {language}.")

    logger.info(f"Building Docker image '{image_name}' from context path '{app_source_dir}'")
    try:
        process = subprocess.run(
            ["docker", "build", "-t", image_name, "."],
            cwd=app_source_dir,
            check=True,
            capture_output=True,
            text=True
        )
        logger.info(f"Docker image build STDOUT for {image_name}:\n{process.stdout}")
        if process.stderr:
            logger.warning(f"Docker image build STDERR for {image_name}:\n{process.stderr}")
        logger.info(f"Docker image '{image_name}' built successfully for app {app_id}.")
        return image_name
    except subprocess.CalledProcessError as e:
        logger.error(f"Docker image build failed for app {app_id} (image: {image_name}). Exit code: {e.returncode}")
        logger.error(f"STDOUT:\n{e.stdout}")
        logger.error(f"STDERR:\n{e.stderr}")
        raise HTTPException(status_code=500, detail=f"Docker image build failed: {e.stderr[:500]}...")
    except FileNotFoundError:
        logger.error("Docker command not found. Please ensure Docker is installed and in PATH.")
        raise HTTPException(status_code=500, detail="Docker command not found on server.")


MAX_CONTEXT_FILES_AI = 7
MAX_FILE_SIZE_AI_BYTES = 15 * 1024
MAX_TOTAL_CONTENT_AI_CHARS = 12000


def get_project_context_for_ai(app_path: str, language: str) -> str:
    context_parts = []
    current_chars_count = 0
    files_read_count = 0

    tree_str = "Project structure (partial):\n"
    tree_lines = 0
    max_tree_lines = 25
    for root, dirs, files in os.walk(app_path):
        dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', '.venv',
                                                'target', 'build', 'dist', '.DS_Store']]
        files = [f for f in files if f not in ['.DS_Store']]

        level = root.replace(app_path, '').count(os.sep)
        if level > 3:
            dirs[:] = []
            continue

        indent = "  " * level
        if tree_lines < max_tree_lines:
            tree_str += f"{indent}{os.path.basename(root) or '.'}/\n"
            tree_lines += 1

        for f_name in files[:4]:
            if tree_lines < max_tree_lines:
                tree_str += f"{indent}  {f_name}\n"
                tree_lines += 1
            else:
                break
        if tree_lines >= max_tree_lines:
            tree_str += f"{indent}  ... (more files/dirs)\n"
            break

    if tree_lines > 0:
        context_parts.append(tree_str)
        current_chars_count += len(tree_str)

    primary_key_files_map = {
        "node": ["package.json"],
        "python": ["requirements.txt", "pyproject.toml", "setup.py"],
        "java": ["pom.xml", "build.gradle"],
    }
    secondary_key_files_map = {
        "node": ["server.js", "app.js", "index.js", "vite.config.js", "next.config.js"],
        "python": ["main.py", "app.py", "wsgi.py", "asgi.py"],
        "java": ["Main.java", "Application.java", "application.properties", "application.yml"],
    }

    candidate_files_ordered = [("README.md", "README.md")]
    for fname in primary_key_files_map.get(language, []):
        candidate_files_ordered.append((fname, fname))
    for fname in secondary_key_files_map.get(language, []):
        candidate_files_ordered.append((fname, fname))

    processed_paths = set()

    def read_and_append(file_path, display_name_hint):
        nonlocal current_chars_count, files_read_count
        if files_read_count >= MAX_CONTEXT_FILES_AI or current_chars_count >= MAX_TOTAL_CONTENT_AI_CHARS:
            return False

        try:
            file_size = os.path.getsize(file_path)
            if 0 < file_size <= MAX_FILE_SIZE_AI_BYTES:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(MAX_FILE_SIZE_AI_BYTES)

                rel_path = os.path.relpath(file_path, app_path)
                header = f"\n--- Content of {rel_path} ---\n"

                if current_chars_count + len(content) + len(header) < MAX_TOTAL_CONTENT_AI_CHARS:
                    context_parts.append(header + content)
                    current_chars_count += len(content) + len(header)
                    files_read_count += 1
                    processed_paths.add(file_path)
                    return True
                else:
                    if files_read_count > 0:
                        return False
        except Exception as e:
            logger.warning(f"Could not read file {file_path} for AI context: {e}")
        return True

    for root, dirs, files in os.walk(app_path, topdown=True):
        dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', '.venv',
                                                'target', 'build', 'dist', '.DS_Store']]

        for cf_name, display_hint in candidate_files_ordered:
            if cf_name in files:
                file_path = os.path.join(root, cf_name)
                if file_path not in processed_paths:
                    if not read_and_append(file_path, display_hint):
                        break
        if files_read_count >= MAX_CONTEXT_FILES_AI or current_chars_count >= MAX_TOTAL_CONTENT_AI_CHARS:
            break

    source_extensions = {
        "python": [".py"], "node": [".js", ".jsx", ".ts", ".tsx"], "java": [".java"]
    }.get(language, [])

    if source_extensions and files_read_count < MAX_CONTEXT_FILES_AI and current_chars_count < MAX_TOTAL_CONTENT_AI_CHARS:
        for root, dirs, files in os.walk(app_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', '.venv',
                                                    'target', 'build', 'dist', '.DS_Store']]
            files.sort(key=lambda name: (len(name), name))

            for f_name in files:
                if any(f_name.endswith(ext) for ext in source_extensions):
                    file_path = os.path.join(root, f_name)
                    if file_path not in processed_paths:
                        if root.replace(app_path, '').count(os.sep) <= 2:
                            if not read_and_append(file_path, f_name):
                                break
            if files_read_count >= MAX_CONTEXT_FILES_AI or current_chars_count >= MAX_TOTAL_CONTENT_AI_CHARS:
                break

    if not context_parts:
        return "No readable project files found or project is empty; cannot provide AI analysis."

    final_context = "".join(context_parts)
    logger.info(f"Generated AI context: ~{current_chars_count} chars, {files_read_count} files for app path {app_path}.")
    return final_context


@app.post("/upload")
async def upload_zip(file: UploadFile = File(...)):
    app_id = str(uuid.uuid4())
    app_dir = os.path.join(BASE_DIR, app_id)
    os.makedirs(app_dir, exist_ok=True)
    zip_path = os.path.join(app_dir, "app.zip")

    logger.info(f"Uploading zip for app_id: {app_id} to {zip_path}")
    try:
        with open(zip_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logger.info(f"Zip file saved: {zip_path}")

        with zipfile.ZipFile(zip_path, "r") as z_ref:
            for member in z_ref.namelist():
                if member.startswith("/") or ".." in member:
                    logger.error(f"Illegal member path '{member}' in zip for app_id {app_id}.")
                    raise HTTPException(status_code=400, detail="Invalid file path in zip archive.")
            z_ref.extractall(app_dir)
        logger.info(f"Zip file extracted to: {app_dir}")
    except zipfile.BadZipFile:
        logger.error(f"Bad zip file uploaded for app_id {app_id}.", exc_info=True)
        if os.path.exists(app_dir):
            shutil.rmtree(app_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid zip archive.")
    except Exception as e:
        logger.error(f"Error during zip upload/extraction for app_id {app_id}: {e}", exc_info=True)
        if os.path.exists(app_dir):
            shutil.rmtree(app_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Failed to process uploaded file: {str(e)}")
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)
            logger.info(f"Zip file removed: {zip_path}")

    return {"app_id": app_id, "message": "File uploaded and extracted successfully."}


@app.post("/clone")
async def clone_repo(req: GitCloneRequest):
    app_id = str(uuid.uuid4())
    app_dir = os.path.join(BASE_DIR, app_id)
    logger.info(f"Received clone request for app_id: {app_id}, repo_url: {req.repo_url}, branch: {req.branch}")

    pat_token = PAT_TOKEN
    effective_clone_url = req.repo_url

    if pat_token:
        logger.info("PAT_TOKEN found in environment.")
        try:
            parsed_url = urlparse(req.repo_url)
            if parsed_url.scheme.lower() == 'https' and parsed_url.hostname and "github.com" in parsed_url.hostname.lower():
                logger.info(f"Attempting to inject PAT into GitHub HTTPS URL: {req.repo_url}")
                encoded_token = quote(pat_token, safe='')
                netloc_with_token = f"{encoded_token}@{parsed_url.hostname}"
                if parsed_url.port:
                    netloc_with_token += f":{parsed_url.port}"
                new_url_parts = (
                    parsed_url.scheme, netloc_with_token, parsed_url.path,
                    parsed_url.params, parsed_url.query, parsed_url.fragment
                )
                effective_clone_url = urlunparse(new_url_parts)
                logger.info(f"URL modified for cloning with PAT. Original: {req.repo_url}")
            else:
                logger.warning(f"PAT_TOKEN found, but repo_url '{req.repo_url}' is not a recognized GitHub HTTPS URL. Cloning with original URL.")
        except Exception as e:
            logger.error(f"Error during URL modification for PAT injection: {e}", exc_info=True)
            logger.warning(f"Falling back to original URL due to parsing error: {req.repo_url}")
    else:
        logger.info("PAT_TOKEN not found in environment. Cloning with original URL.")

    branches_to_try_set = {req.branch}
    if req.branch == "main":
        branches_to_try_set.add("master")
    elif req.branch == "master":
        branches_to_try_set.add("main")
    branches_to_try = list(branches_to_try_set)

    last_git_error_stderr = ""
    cloned_successfully = False
    final_cloned_branch = None

    for branch_attempt in branches_to_try:
        logger.info(f"Attempting to clone branch: '{branch_attempt}' for app_id: {app_id}")
        if os.path.exists(app_dir):
            shutil.rmtree(app_dir)
        os.makedirs(app_dir, exist_ok=True)

        try:
            Repo.clone_from(effective_clone_url, app_dir, branch=branch_attempt)
            logger.info(f"Successfully cloned '{req.repo_url}' on branch '{branch_attempt}' into {app_dir}")
            cloned_successfully = True
            final_cloned_branch = branch_attempt
            break
        except GitCommandError as e:
            logger.error(f"GitCommandError while cloning (branch: {branch_attempt}): {e.stderr}", exc_info=False)
            last_git_error_stderr = e.stderr or str(e)
            if "could not read Username" in last_git_error_stderr.lower():
                logger.warning(f"Git tried to prompt for username. Stopping.")
                break
            elif "authentication failed" in last_git_error_stderr.lower():
                logger.warning(f"Authentication failed. Stopping.")
                break
            elif "repository not found" in last_git_error_stderr.lower() and not ("authentication failed" in last_git_error_stderr.lower()):
                logger.warning(f"Repository not found. Stopping.")
                break
            elif "couldn't find remote ref" in last_git_error_stderr.lower() or "not found" in last_git_error_stderr.lower():
                logger.info(f"Branch '{branch_attempt}' not found, trying next if available.")
                if branch_attempt == branches_to_try[-1]:
                    logger.warning(f"All specified branches not found.")
                continue
            else:
                logger.warning(f"Unhandled GitCommandError on branch {branch_attempt}. Stopping.")
                break
        except Exception as e:
            logger.exception(f"Unexpected error during cloning: {e}")
            if os.path.exists(app_dir):
                shutil.rmtree(app_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Unexpected error during cloning: {str(e)}")

    if cloned_successfully:
        return {"app_id": app_id, "cloned_branch": final_cloned_branch, "message": "Repository cloned successfully."}
    else:
        detail_msg_base = f"Failed to clone from repository {req.repo_url} on tried branch(es): {', '.join(branches_to_try)}."
        if last_git_error_stderr:
            if "could not read Username" in last_git_error_stderr.lower():
                detail_msg = f"{detail_msg_base} Authentication via PAT failed. Last Git error: {last_git_error_stderr}"
                status_code = 500
            elif "authentication failed" in last_git_error_stderr.lower():
                detail_msg = f"{detail_msg_base} Authentication failed. Last Git error: {last_git_error_stderr}"
                status_code = 403
            elif "repository not found" in last_git_error_stderr.lower():
                detail_msg = f"{detail_msg_base} Repository not found. Last Git error: {last_git_error_stderr}"
                status_code = 404
            else:
                detail_msg = f"{detail_msg_base} Last Git error: {last_git_error_stderr}"
                status_code = 500
        else:
            detail_msg = detail_msg_base + " No specific Git error captured."
            status_code = 404

        logger.error(f"All clone attempts failed. Final detail: {detail_msg}")
        if os.path.exists(app_dir):
            shutil.rmtree(app_dir, ignore_errors=True)
        raise HTTPException(status_code=status_code, detail=detail_msg)


@app.post("/instrument")
async def instrument_app(req: InstrumentRequest):
    app_dir = os.path.join(BASE_DIR, req.app_id)
    logger.info(f"Instrumenting app: {req.app_id} in dir: {app_dir}")

    if not os.path.isdir(app_dir):
        logger.error(f"App directory not found for instrumentation: {app_dir}")
        raise HTTPException(status_code=404, detail="App not found. Please upload or clone first.")

    lang = detect_language(app_dir)
    logger.info(f"Detected language for app {req.app_id}: {lang}")
    if lang == "unknown":
        logger.warning(f"Unsupported or undetectable language for app {req.app_id}")
        raise HTTPException(status_code=400, detail="Unsupported language or unable to detect language.")

    port = PORT_MAP.get(lang, 8080)

    try:
        image_name = build_user_image(req.app_id, app_dir, lang)
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Unexpected error during image build: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Unexpected error during image build: {str(e)}")

    try:
        jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    except Exception as e:
        logger.error(f"Failed to init Jinja2 environment: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server config error: Jinja2 env.")

    k8s_app_name = f"user-app-{req.app_id.lower()}"
    context = {
        "app_id_normalized": req.app_id.lower().replace("_", "-"),
        "k8s_app_name": k8s_app_name,
        "image": image_name,
        "port": port,
        "language": lang,
        "app_id": req.app_id
    }

    manifests_applied = []
    for template_filename, output_filename_pattern in [
        ("deployment.yaml.j2", f"{k8s_app_name}-deployment.yaml"),
        ("service.yaml.j2",    f"{k8s_app_name}-service.yaml")
    ]:
        try:
            template = jinja_env.get_template(template_filename)
            rendered_content = template.render(**context)
        except Exception as e:
            logger.error(f"Failed to render template '{template_filename}': {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to render template {template_filename}: {str(e)}")

        output_path = os.path.join(K8S_OUTPUT_DIR, output_filename_pattern)
        try:
            with open(output_path, "w") as f:
                f.write(rendered_content)
            logger.info(f"Rendered manifest '{output_filename_pattern}' to {output_path}")
        except IOError as e:
            logger.error(f"Failed to write manifest to {output_path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to write manifest file: {str(e)}")

        try:
            kubectl_command = ["kubectl", "apply", "-n", "traceassist", "-f", output_path]
            logger.info(f"Running kubectl command: {' '.join(kubectl_command)}")
            process = subprocess.run(
                kubectl_command,
                check=True, capture_output=True, text=True, timeout=60
            )
            logger.info(f"kubectl apply STDOUT for {output_filename_pattern}:\n{process.stdout}")
            if process.stderr:
                logger.warning(f"kubectl apply STDERR for {output_filename_pattern}:\n{process.stderr}")
            manifests_applied.append(output_filename_pattern)
        except subprocess.CalledProcessError as e:
            stderr_msg = e.stderr.strip() if e.stderr else "No stderr."
            stdout_msg = e.stdout.strip() if e.stdout else "No stdout."
            logger.error(
                f"kubectl apply failed for {output_filename_pattern}. Exit code: {e.returncode}\nSTDOUT: {stdout_msg}\nSTDERR: {stderr_msg}"
            )
            raise HTTPException(status_code=500, detail=f"Failed to apply manifest {output_filename_pattern}: {stderr_msg[:500]}...")
        except FileNotFoundError:
            logger.error("kubectl not found.")
            raise HTTPException(status_code=500, detail="kubectl command not found.")
        except subprocess.TimeoutExpired:
            logger.error(f"kubectl apply timed out for {output_filename_pattern}.")
            raise HTTPException(status_code=500, detail=f"kubectl apply timed out for {output_filename_pattern}.")

    return {
        "message": f"Application {req.app_id} instrumented and deployment initiated.",
        "app_id": req.app_id,
        "image_built": image_name,
        "manifests_applied": manifests_applied,
        "k8s_app_name": k8s_app_name
    }


@app.post("/run")
async def run_app_status(req: InstrumentRequest):
    k8s_app_name = f"user-app-{req.app_id.lower()}"
    logger.info(f"Received /run request for app_id {req.app_id}.")
    return {
        "message": f"Application {req.app_id} (K8s name: {k8s_app_name}) is expected to be running or deploying.",
        "app_id": req.app_id,
        "k8s_app_name": k8s_app_name,
        "note": "Check Kubernetes for status."
    }


@app.post("/suggestions", response_model=AISuggestionResponse)
async def ai_code_analysis(req: InstrumentRequest):
    if not openai_client:
        logger.error("OpenAI client not configured.")
        raise HTTPException(status_code=503, detail="AI service unavailable.")
    app_id = req.app_id
    app_dir = os.path.join(BASE_DIR, app_id)
    logger.info(f"Performing AI code analysis for app: {app_id} in dir: {app_dir}")

    if not os.path.isdir(app_dir):
        logger.error(f"App directory not found for AI analysis: {app_dir}")
        raise HTTPException(status_code=404, detail="App not found for AI analysis.")

    language = detect_language(app_dir)
    project_context = get_project_context_for_ai(app_dir, language)

    prompt = f"""
You are an expert software observability and deployment assistant.
Application with app_id '{app_id}' (K8s name: 'user-app-{app_id.lower()}') is identified as a '{language}' project.
Context:
{project_context}
"""
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an expert software development and observability assistant."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=600
        )
        suggestion_text = response.choices[0].message.content.strip()
        model_used = response.model if response.model else "unknown_model"
        logger.info(f"Received AI suggestions for app {app_id} using model {model_used}")
        return AISuggestionResponse(app_id=app_id, suggestions=suggestion_text, model_used=model_used)
    except APIConnectionError as e:
        logger.error(f"OpenAI API connection error for app {app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="AI service connection error.")
    except RateLimitError as e:
        logger.error(f"OpenAI API rate limit exceeded: {e}")
        raise HTTPException(status_code=429, detail="AI service rate limit exceeded.")
    except APIStatusError as e:
        err_detail_msg = str(e)
        status_code_from_api = getattr(e, 'status_code', 500)
        try:
            err_detail_json = e.response.json()
            err_detail_msg = err_detail_json.get("error", {}).get("message", err_detail_msg)
        except Exception:
            pass
        logger.error(f"OpenAI API status error: {err_detail_msg}")
        raise HTTPException(status_code=status_code_from_api, detail=f"AI service API error: {err_detail_msg}")
    except Exception as e:
        logger.error(f"Unexpected error during AI analysis: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Unexpected error during AI analysis.")


@app.post("/alert")
async def handle_alert(req: Request):
    if not jira:
        logger.error("Jira client not configured.")
        raise HTTPException(status_code=500, detail="Jira not configured.")

    payload = await req.json()
    alert_name = payload.get("alertName", "unknown-alert")
    fired_at_ms = int(payload.get("firedAt", 0))
    labels = payload.get("labels", {})
    service = labels.get("service", "unknown")

    start_ns = (fired_at_ms - 30_000) * 1_000_000
    end_ns = (fired_at_ms + 30_000) * 1_000_000
    loki_query = f'{{service="{service}"}} |= "ERROR"'

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{LOKI_ENDPOINT}/loki/api/v1/query_range",
            params={"query": loki_query, "start": start_ns, "end": end_ns},
            timeout=10.0
        )
    if resp.status_code != 200:
        logger.error(f"Loki query failed: {resp.text}")
        raise HTTPException(status_code=502, detail="Failed to query Loki.")

    streams = resp.json().get("data", {}).get("result", [])
    logs = []
    for stream in streams:
        for ts, line in stream.get("values", []):
            logs.append(line)
    logs_text = "\n".join(logs) or "<no logs found>"

    issue = jira.issue_create(
        project=JIRA_PROJECT_KEY,
        summary=f"[Alert] {alert_name} on {service}",
        description=(
            f"**Alert:** `{alert_name}` fired at `{fired_at_ms}` for service `{service}`\n\n"
            f"**Payload:**\n```\n{payload}\n```\n\n"
            f"**Logs:**\n```\n{logs_text}\n```"
        )
    )

    return {"issue_key": issue["key"]}
