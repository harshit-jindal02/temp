import os
import shutil
import zipfile
import uuid
import subprocess
import logging
from urllib.parse import urlparse, urlunparse, quote # Added quote

from fastapi import FastAPI, File, UploadFile, HTTPException
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

# Load environment
load_dotenv() # This will load .env file if present
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# PAT_TOKEN will be read from env inside the /clone endpoint
# OTEL_EXPORTER_OTLP_ENDPOINT will be read from env for OTel setup

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not OPENAI_API_KEY and not openai_client:
    logging.warning("OPENAI_API_KEY not found. AI suggestions will be unavailable.")


# OpenTelemetry setup
# OTEL_ENDPOINT is read from env: OTEL_EXPORTER_OTLP_ENDPOINT (from deployment) or SIGNOZ_CLOUD_ENDPOINT
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", os.getenv("SIGNOZ_CLOUD_ENDPOINT"))
SERVICE_NAME = "traceassist-backend"

if OTEL_ENDPOINT:
    logging.info(f"OpenTelemetry configured with endpoint: {OTEL_ENDPOINT}")
    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    # Assuming insecure=True is fine for your Signoz cloud endpoint if it's gRPC without TLS on 4317
    # For HTTPS (typically 4318 for gRPC), insecure should be False and certs handled.
    # Your deployment uses ingest.in.signoz.cloud:4317 which is typically gRPC HTTP/2 without client TLS.
    exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
else:
    logging.warning("OTEL_EXPORTER_OTLP_ENDPOINT not found. OpenTelemetry tracing will be disabled.")
    provider = None # Ensure provider is None if OTel is not set up

# FastAPI setup
app = FastAPI()

if provider: # Instrument only if OTel provider is configured
    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
else:
    logging.info("Skipping FastAPI OpenTelemetry instrumentation as provider is not configured.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"], # Adjust for your frontend URL if different
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Logging
# BasicConfig should ideally be called only once.
# If other modules also call it, it might not have the desired effect or could be overridden.
# For FastAPI, Uvicorn's logger might also be in play.
# This setup is generally fine for a single-file app.
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Directories
BASE_DIR = "user-apps"
TEMPLATE_DIR = "templates" # For Jinja2 templates for K8s manifests
K8S_OUTPUT_DIR = "k8s-generated" # For storing generated K8s manifests

os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(TEMPLATE_DIR, exist_ok=True) # Ensure your Jinja templates are here
os.makedirs(K8S_OUTPUT_DIR, exist_ok=True)


# --- Pydantic Models ---
class GitCloneRequest(BaseModel):
    repo_url: str = Field(..., description="The HTTPS URL of the repository (e.g., https://github.com/user/repo.git)")
    branch: Optional[str] = Field(default="main", description="The branch to clone. Defaults to 'main'. 'master' is normalized to 'main'.")

    @validator("branch", pre=True, always=True)
    @classmethod
    def normalize_default_branch(cls, v: Any) -> str:
        if v is None:
            # logger.debug("Branch input is None, defaulting to 'main'.") # logger might not be init yet
            return "main"
        if isinstance(v, str):
            original_v = v
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
        # Allow both https and http for flexibility, token injection only for https://github.com
        if not (v.startswith("https://") or v.startswith("http://")):
             raise ValueError("repo_url must be an HTTP or HTTPS URL.")
        # Specific check for github.com if we want to be strict about token injection target
        # if v.startswith("https://") and "github.com" not in v:
        #     logger.warning("Cloning a non-GitHub HTTPS repo. PAT token will not be injected.")
        return v

class InstrumentRequest(BaseModel):
    app_id: str

class AISuggestionResponse(BaseModel):
    app_id: str
    suggestions: str # Keep as single string as per your existing model
    model_used: Optional[str] = None


# --- Helper Functions ---
def detect_language(app_path: str) -> str:
    # (Your existing detect_language function - seems fine)
    has_package_json = False; py_count = 0; java_count = 0

    if not os.path.isdir(app_path):
        logger.warning(f"Path provided to detect_language is not a directory: {app_path}")
        return "unknown"

    # Common directories to exclude from language detection scan
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
            if f_name.endswith(".jar"):
                jar_count += 1
            # Add other file types if needed, e.g., .go, .rb, .php

    if has_package_json: # Node.js (package.json is a strong indicator)
        return "nodejs"
    # Prioritize Python if it's clearly Python and not mixed with Java
    if py_count > 0 and java_count == 0:
        return "python"
    # Then Java
    if java_count > 0:
        return "java"
    # Fallback if Python files were present but also Java (less common for primary lang)
    if py_count > 0:
        return "python"
    # return 
        
    logger.info(f"Language detection for {app_path}: Node.js (package.json): {has_package_json}, Python files: {py_count}, Java files: {java_count}. Result: unknown")
    return "unknown"

PORT_MAP = {"nodejs": 3000, "python": 8000, "java": 8080, "unknown": 8080} # Default for unknown

MAX_CONTEXT_FILES_AI = 7  # Max files to read content from
MAX_FILE_SIZE_AI_BYTES = 15 * 1024  # 15 KB per file
MAX_TOTAL_CONTENT_AI_CHARS = 12000

def generate_dockerfile(app_path: str, language: str, app_id: str) -> Optional[str]:
    # (Your existing generate_dockerfile function - make sure it outputs to app_path)
    # It should create a Dockerfile *inside* the app_path (e.g., user-apps/{app_id}/Dockerfile)
    dockerfile_content = None
    # Use a port from PORT_MAP, or a default if language not in map
    port = PORT_MAP.get(language, 8080)

    if language == "nodejs":
        # Basic Node.js Dockerfile, assumes 'npm start' is the run command
        # and package.json lists dependencies.
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
# Common run commands, adjust if your projects use something else
# CMD [ "node", "index.js" ]
# CMD [ "node", "server.js" ]
# CMD [ "node", "app.js" ]
CMD [ "npm", "start" ]
"""
    elif language == "python":
        # Basic Python Dockerfile, assumes FastAPI/Uvicorn and requirements.txt
        # Assumes main.py has 'app' instance: `CMD ["uvicorn", "main:app"...]`
        dockerfile_content = f"""
FROM python:3.10-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE {port}
CMD [ "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{str(port)}" ]
"""
    elif language == "java":
        # Basic Java Spring Boot Dockerfile (Maven example)
        # Assumes a fat JAR is built.
        dockerfile_content = f"""
FROM maven:3.8-openjdk-17 AS build
WORKDIR /app
COPY pom.xml .
# RUN mvn dependency:go-offline -B # Optional: download dependencies first
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
        return None # Or raise HTTPException

    if dockerfile_content:
        dockerfile_path = os.path.join(app_path, "Dockerfile")
        try:
            with open(dockerfile_path, "w") as f:
                f.write(dockerfile_content.strip())
            logger.info(f"Dockerfile generated for app {app_id} ({language}) at {dockerfile_path}")
            return dockerfile_path
        except IOError as e:
            logger.error(f"Failed to write Dockerfile for app {app_id} at {dockerfile_path}: {e}", exc_info=True)
            return None
    return None


def build_user_image(app_id: str, app_source_dir: str, language: str) -> str:
    # (Your existing build_user_image - ensure Dockerfile is generated first correctly)
    image_name = f"user-app-{app_id.lower()}:latest" # Docker image names often lowercase

    logger.info(f"Attempting to generate Dockerfile for app {app_id} ({language}) in {app_source_dir}")
    dockerfile_path = generate_dockerfile(app_source_dir, language, app_id)

    if not dockerfile_path or not os.path.exists(dockerfile_path):
        logger.error(f"Dockerfile generation failed or Dockerfile not found for app {app_id}.")
        raise HTTPException(status_code=500, detail=f"Dockerfile generation failed for language {language}.")

    logger.info(f"Building Docker image '{image_name}' from context path '{app_source_dir}'")
    try:
        # Using subprocess.run for better error handling and output capture
        process = subprocess.run(
            ["docker", "build", "-t", image_name, "."], # Build context is app_source_dir
            cwd=app_source_dir, # Run docker build from within the app's directory
            check=True,         # Raise CalledProcessError on non-zero exit
            capture_output=True,# Capture stdout/stderr
            text=True           # Decode stdout/stderr as text
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
        raise HTTPException(status_code=500, detail=f"Docker image build failed: {e.stderr[:500]}...") # Show first 500 chars of error
    except FileNotFoundError: # docker command not found
        logger.error("Docker command not found. Please ensure Docker is installed and in PATH.")
        raise HTTPException(status_code=500, detail="Docker command not found on server.")


def get_project_context_for_ai(app_path: str, language: str) -> str:
    context_parts = []
    current_chars_count = 0
    files_read_count = 0

    # 1. File tree (limited)
    tree_str = "Project structure (partial):\n"
    tree_lines = 0
    max_tree_lines = 25
    for root, dirs, files in os.walk(app_path):
        dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', '.venv', 'target', 'build', 'dist', '.DS_Store']]
        files = [f for f in files if f not in ['.DS_Store']]
        
        level = root.replace(app_path, '').count(os.sep)
        if level > 3: # Limit directory depth in tree
            dirs[:] = [] # Don't look deeper
            continue

        indent = "  " * level
        if tree_lines < max_tree_lines:
            tree_str += f"{indent}{os.path.basename(root) or '.'}/\n"
            tree_lines +=1
        
        for f_name in files[:4]: # Limit files per dir in tree display
            if tree_lines < max_tree_lines:
                tree_str += f"{indent}  {f_name}\n"
                tree_lines += 1
            else: break
        if tree_lines >= max_tree_lines:
            tree_str += f"{indent}  ... (more files/dirs)\n"
            break 
    
    if tree_lines > 0 :
        context_parts.append(tree_str)
        current_chars_count += len(tree_str)

    # 2. Key file contents
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
    for fname in primary_key_files_map.get(language, []): candidate_files_ordered.append((fname, fname))
    for fname in secondary_key_files_map.get(language, []): candidate_files_ordered.append((fname, fname))

    processed_paths = set()
    
    def read_and_append(file_path, display_name_hint):
        nonlocal current_chars_count, files_read_count
        if files_read_count >= MAX_CONTEXT_FILES_AI or current_chars_count >= MAX_TOTAL_CONTENT_AI_CHARS:
            return False 

        try:
            file_size = os.path.getsize(file_path)
            if 0 < file_size <= MAX_FILE_SIZE_AI_BYTES:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(MAX_FILE_SIZE_AI_BYTES) # Read up to max size
                
                rel_path = os.path.relpath(file_path, app_path)
                header = f"\n--- Content of {rel_path} ---\n"
                
                if current_chars_count + len(content) + len(header) < MAX_TOTAL_CONTENT_AI_CHARS:
                    context_parts.append(header + content)
                    current_chars_count += len(content) + len(header)
                    files_read_count += 1
                    processed_paths.add(file_path)
                    return True
                else: 
                    if files_read_count > 0: return False 
        except Exception as e:
            logger.warning(f"Could not read file {file_path} for AI context: {e}")
        return True 

    for root, dirs, files in os.walk(app_path, topdown=True):
        dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', '.venv', 'target', 'build', 'dist', '.DS_Store']]
        
        for cf_name, display_hint in candidate_files_ordered:
            if cf_name in files:
                file_path = os.path.join(root, cf_name)
                if file_path not in processed_paths:
                    if not read_and_append(file_path, display_hint): break 
        if files_read_count >= MAX_CONTEXT_FILES_AI or current_chars_count >= MAX_TOTAL_CONTENT_AI_CHARS: break
    
    source_extensions = {
        "python": [".py"], "node": [".js", ".jsx", ".ts", ".tsx"], "java": [".java"]
    }.get(language, [])

    if source_extensions and files_read_count < MAX_CONTEXT_FILES_AI and current_chars_count < MAX_TOTAL_CONTENT_AI_CHARS:
        for root, dirs, files in os.walk(app_path, topdown=True):
            dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', '.venv', 'target', 'build', 'dist', '.DS_Store']]
            files.sort(key=lambda name: (len(name), name)) 

            for f_name in files:
                if any(f_name.endswith(ext) for ext in source_extensions):
                    file_path = os.path.join(root, f_name)
                    if file_path not in processed_paths:
                        if root.replace(app_path, '').count(os.sep) <= 2: # Prefer files not too deep
                            if not read_and_append(file_path, f_name): break 
            if files_read_count >= MAX_CONTEXT_FILES_AI or current_chars_count >= MAX_TOTAL_CONTENT_AI_CHARS: break
                
    if not context_parts:
        return "No readable project files found or project is empty; cannot provide AI analysis."
        
    final_context = "".join(context_parts) 
    logger.info(f"Generated AI context: ~{current_chars_count} chars, {files_read_count} files for app path {app_path}.")
    return final_context
# --- API Endpoints ---

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
            # Sanitize member names to prevent directory traversal (Zip Slip)
            for member in z_ref.namelist():
                # Check for path traversal attempts
                if member.startswith("/") or ".." in member:
                    logger.error(f"Illegal member path '{member}' in zip for app_id {app_id}.")
                    raise HTTPException(status_code=400, detail="Invalid file path in zip archive.")
            z_ref.extractall(app_dir) # Extract all sanitized members

        logger.info(f"Zip file extracted to: {app_dir}")
    except zipfile.BadZipFile:
        logger.error(f"Bad zip file uploaded for app_id {app_id}.", exc_info=True)
        if os.path.exists(app_dir): shutil.rmtree(app_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid zip archive.")
    except Exception as e:
        logger.error(f"Error during zip upload/extraction for app_id {app_id}: {e}", exc_info=True)
        if os.path.exists(app_dir): shutil.rmtree(app_dir, ignore_errors=True)
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

    pat_token = os.getenv("PAT_TOKEN") # This is the environment variable from your K8s secret
    effective_clone_url = req.repo_url

    if pat_token:
        logger.info("PAT_TOKEN found in environment.")
        try:
            parsed_url = urlparse(req.repo_url)
            # Only inject token for github.com HTTPS URLs
            if parsed_url.scheme.lower() == 'https' and parsed_url.hostname and "github.com" in parsed_url.hostname.lower():
                logger.info(f"Attempting to inject PAT into GitHub HTTPS URL: {req.repo_url}")
                encoded_token = quote(pat_token, safe='') # URL-encode token
                
                netloc_with_token = f"{encoded_token}@{parsed_url.hostname}"
                if parsed_url.port:
                    netloc_with_token += f":{parsed_url.port}"
                
                new_url_parts = (
                    parsed_url.scheme, netloc_with_token, parsed_url.path,
                    parsed_url.params, parsed_url.query, parsed_url.fragment
                )
                effective_clone_url = urlunparse(new_url_parts)
                # SECURITY: Avoid logging the full effective_clone_url in production if it contains sensitive tokens.
                logger.info(f"URL modified for cloning with PAT. Original: {req.repo_url}")
            else:
                logger.warning(f"PAT_TOKEN found, but repo_url '{req.repo_url}' is not a recognized GitHub HTTPS URL for token injection. Cloning with original URL.")
        except Exception as e:
            logger.error(f"Error during URL modification for PAT injection: {e}", exc_info=True)
            logger.warning(f"Falling back to original URL due to parsing error: {req.repo_url}")
            # effective_clone_url remains req.repo_url
    else:
        logger.info("PAT_TOKEN not found in environment. Cloning with original URL. This may fail for private repositories.")

    # Branch handling: try requested, then common fallbacks if it's main/master
    branches_to_try_set = {req.branch} # Start with the specifically requested branch
    if req.branch == "main":
        branches_to_try_set.add("master")
    elif req.branch == "master":
        branches_to_try_set.add("main")
    branches_to_try = list(branches_to_try_set)


    last_git_error_stderr = ""
    cloned_successfully = False
    final_cloned_branch = None

    for branch_attempt in branches_to_try:
        logger.info(f"Attempting to clone branch: '{branch_attempt}' for app_id: {app_id} using effective URL.")
        # logger.debug(f"Git clone command will use URL: {effective_clone_url}") # UNCOMMENT FOR DEEP DEBUG ONLY

        if os.path.exists(app_dir):
            shutil.rmtree(app_dir)
        os.makedirs(app_dir, exist_ok=True)

        try:
            Repo.clone_from(effective_clone_url, app_dir, branch=branch_attempt)
            logger.info(f"Successfully cloned '{req.repo_url}' on branch '{branch_attempt}' into {app_dir}")
            cloned_successfully = True
            final_cloned_branch = branch_attempt
            break # Exit loop on success
        except GitCommandError as e:
            logger.error(f"GitCommandError while cloning (branch: {branch_attempt}): {e.stderr}", exc_info=False)
            last_git_error_stderr = e.stderr or str(e)
            
            # More specific error checks
            if "could not read Username" in last_git_error_stderr.lower():
                logger.warning(f"Git tried to prompt for username (branch {branch_attempt}). This indicates PAT injection might have failed or PAT is invalid. Stopping.")
                break # Fatal for this attempt, likely PAT issue
            elif "authentication failed" in last_git_error_stderr.lower():
                logger.warning(f"Authentication failed (branch {branch_attempt}). PAT might be incorrect or lack permissions. Stopping.")
                break # Fatal for this attempt
            elif "repository not found" in last_git_error_stderr.lower() and not ("authentication failed" in last_git_error_stderr.lower()):
                logger.warning(f"Repository not found (branch {branch_attempt}). URL or repo name might be incorrect. Stopping.")
                break # Fatal for this attempt
            elif "couldn't find remote ref" in last_git_error_stderr.lower() or "not found" in last_git_error_stderr.lower(): # Branch not found
                logger.info(f"Branch '{branch_attempt}' not found for {req.repo_url}, trying next if available.")
                if branch_attempt == branches_to_try[-1]: # If this was the last branch to try
                    logger.warning(f"All specified branches ({', '.join(branches_to_try)}) not found.")
                continue # Try next branch
            else: # Other Git error
                logger.warning(f"Unhandled GitCommandError on branch {branch_attempt}. Stopping further branch attempts.")
                break
        except Exception as e:
            logger.exception(f"Unexpected error during cloning of {req.repo_url} (branch: {branch_attempt}).")
            if os.path.exists(app_dir): shutil.rmtree(app_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Unexpected error during cloning: {str(e)}")

    if cloned_successfully:
        return {"app_id": app_id, "cloned_branch": final_cloned_branch, "message": "Repository cloned successfully."}
    else:
        # Construct detailed error message if cloning failed
        detail_msg_base = f"Failed to clone from repository {req.repo_url} on tried branch(es): {', '.join(branches_to_try)}."
        status_code = 400 # Default

        if last_git_error_stderr:
            if "could not read Username" in last_git_error_stderr.lower():
                detail_msg = f"{detail_msg_base} Git tried to prompt for username, indicating authentication via PAT in URL failed or was not attempted. Check PAT_TOKEN. Last Git error: {last_git_error_stderr}"
                status_code = 500 # Internal server / config error
            elif "authentication failed" in last_git_error_stderr.lower():
                detail_msg = f"{detail_msg_base} Authentication failed. Ensure PAT_TOKEN is correct and has repository access. Last Git error: {last_git_error_stderr}"
                status_code = 403
            elif "repository not found" in last_git_error_stderr.lower() and not ("authentication failed" in last_git_error_stderr.lower()):
                detail_msg = f"{detail_msg_base} Repository not found or access denied. Last Git error: {last_git_error_stderr}"
                status_code = 404
            else:
                detail_msg = f"{detail_msg_base} Last Git error: {last_git_error_stderr}"
                status_code = 500 # Generic Git error
        else:
            detail_msg = detail_msg_base + " No specific Git error captured, branch(es) might not exist."
            status_code = 404 # If no git error but still failed, likely branch not found

        logger.error(f"All clone attempts failed for {req.repo_url}. Final detail: {detail_msg}")
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
        logger.warning(f"Unsupported or undetectable language for app {req.app_id} in {app_dir}")
        raise HTTPException(status_code=400, detail="Unsupported language or unable to detect language for Dockerfile generation.")

    port = PORT_MAP.get(lang, 8080) # Get port, default if lang not in map

    # Build the Docker image (generate_dockerfile is called within build_user_image)
    try:
        image_name = build_user_image(req.app_id, app_dir, lang) # Pass app_dir as source
    except HTTPException as e: # Pass through HTTPExceptions from build_user_image
        raise e
    except Exception as e: # Catch other unexpected errors from build
        logger.error(f"Unexpected error during image build for app {req.app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred during image build: {str(e)}")

    # Render & apply k8s manifests
    # Ensure TEMPLATE_DIR is correctly set and contains your Jinja2 templates
    # e.g., templates/deployment.yaml.j2, templates/service.yaml.j2
    try:
        jinja_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    except Exception as e:
        logger.error(f"Failed to initialize Jinja2 environment from TEMPLATE_DIR '{TEMPLATE_DIR}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server configuration error: Jinja2 template environment.")

    # Context for K8s templates
    # Ensure your service name in K8s matches how you want to expose it.
    # The image_name is already fully qualified (e.g., user-app-app_id:latest)
    k8s_app_name = f"user-app-{req.app_id.lower()}" # Consistent K8s naming
    context = {
        "app_id_normalized": req.app_id.lower().replace("_","-"), # K8s names often use hyphens
        "k8s_app_name": k8s_app_name,
        "image": image_name,
        "port": port,
        "language": lang,
        "app_id": req.app_id # Original app_id if needed
    }
    
    
    logger.info(f"Context for K8s templates for app {req.app_id}: {context}")

    manifests_applied = []
    for template_filename, output_filename_pattern in [
        ("deployment.yaml.j2", f"{k8s_app_name}-deployment.yaml"),
        ("service.yaml.j2",    f"{k8s_app_name}-service.yaml")
    ]:
        try:
            template = jinja_env.get_template(template_filename)
            rendered_content = template.render(**context)
        except Exception as e: # Catch Jinja errors (template not found, rendering error)
            logger.error(f"Failed to render K8s template '{template_filename}' for app {req.app_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to render K8s template {template_filename}: {str(e)}")

        output_path = os.path.join(K8S_OUTPUT_DIR, output_filename_pattern)
        try:
            with open(output_path, "w") as f:
                f.write(rendered_content)
            logger.info(f"Rendered K8s manifest '{output_filename_pattern}' to {output_path} for app {req.app_id}")
        except IOError as e:
            logger.error(f"Failed to write K8s manifest to {output_path} for app {req.app_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Failed to write K8s manifest file: {str(e)}")

        # Apply the manifest using kubectl
        try:
            # Ensure kubectl is in PATH and configured to talk to your K8s cluster
            # The namespace 'traceassist' is hardcoded here as per your deployment.
            # Consider making namespace configurable if needed.
            kubectl_command = ["kubectl", "apply", "-n", "traceassist", "-f", output_path]
            logger.info(f"Running kubectl command: {' '.join(kubectl_command)}")
            process = subprocess.run(
                kubectl_command,
                check=True, capture_output=True, text=True, timeout=60 # 60s timeout for kubectl
            )
            logger.info(f"kubectl apply STDOUT for {output_filename_pattern}:\n{process.stdout}")
            if process.stderr:
                 logger.warning(f"kubectl apply STDERR for {output_filename_pattern}:\n{process.stderr}")
            manifests_applied.append(output_filename_pattern)
        except subprocess.CalledProcessError as e:
            stderr_msg = e.stderr.strip() if e.stderr else "No stderr."
            stdout_msg = e.stdout.strip() if e.stdout else "No stdout."
            logger.error(f"kubectl apply failed for {output_filename_pattern}. Exit code: {e.returncode}\nSTDOUT: {stdout_msg}\nSTDERR: {stderr_msg}", exc_info=False)
            raise HTTPException(status_code=500, detail=f"Failed to apply K8s manifest {output_filename_pattern}: {stderr_msg[:500]}...")
        except FileNotFoundError: # kubectl command not found
            logger.error("kubectl command not found. Please ensure kubectl is installed and in PATH.")
            raise HTTPException(status_code=500, detail="kubectl command not found on server.")
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


@app.post("/run") # This endpoint is now more of a confirmation if /instrument deploys
async def run_app_status(req: InstrumentRequest):
    # In a K8s setup, /instrument handles the deployment.
    # This endpoint could be used to check deployment status in the future.
    # For now, it's a placeholder.
    k8s_app_name = f"user-app-{req.app_id.lower()}"
    logger.info(f"Received /run request for app_id {req.app_id}. App should be deploying/running as {k8s_app_name} in K8s.")
    return {
        "message": f"Application {req.app_id} (K8s name: {k8s_app_name}) is expected to be running or deploying via Kubernetes.",
        "app_id": req.app_id,
        "k8s_app_name": k8s_app_name,
        "note": "Deployment is handled by the /instrument endpoint. Check Kubernetes for status."
    }


@app.post("/suggestions", response_model=AISuggestionResponse)
async def ai_code_analysis(req: InstrumentRequest):
    if not openai_client:
        logger.error("OpenAI client not configured. OPENAI_API_KEY missing.")
        raise HTTPException(status_code=503, detail="AI service unavailable: OpenAI API key not configured.")

    app_id = req.app_id
    app_dir = os.path.join(BASE_DIR, app_id)
    logger.info(f"Performing AI code analysis for app: {app_id} in dir: {app_dir}")

    if not os.path.isdir(app_dir):
        logger.error(f"App directory not found for AI analysis: {app_dir}")
        raise HTTPException(status_code=404, detail="App not found for AI analysis. Please upload or clone first.")

    language = detect_language(app_dir)
    k8s_app_name = f"user-app-{req.app_id.lower()}" # For context in prompt

    project_context = get_project_context_for_ai(app_dir, language)

    if "No readable project files" in project_context or "project is empty" in project_context :
         raise HTTPException(status_code=400, detail="Could not gather sufficient project context for AI analysis. Project might be empty or unreadable.")
    # Simplified prompt, as detailed context gathering was removed from this version of the code.
    # If you re-add `get_project_context_for_ai`, integrate its output here.
    prompt_lang_desc = language if language != "unknown" else "an application"
    prompt = f"""
    You are an expert software observability and deployment assistant.
    An application with app_id '{app_id}' (likely deployed in Kubernetes as '{k8s_app_name}')
    has been identified as a '{prompt_lang_desc}' project.

    Please provide concise, practical, and brief suggestions related to the following points.
    Format your response clearly using Markdown.
    Use Markdown headings (e.g., '### Title') for section titles.
    Use **bold text** for emphasizing key terms within explanations.
    Use Markdown lists (e.g., starting lines with '-' or '1.') for steps or items within each section.

    IMPORTANT: Do NOT prefix any lines, especially headings or list items, with bullet characters like '•'. Only use standard Markdown list markers (like '-' or '1.') at the beginning of list item lines.

    ### 1. Key Observability Metrics to Monitor for {prompt_lang_desc}
    (List 3-5 key metrics with brief explanations using a Markdown list. Emphasize metric names in bold.)

    ### 2. Common Troubleshooting Steps if the Application Fails in Kubernetes
    (List 3-5 common steps using a Markdown list.)

    ### 3. One Best Practice for Logging in a Containerized {prompt_lang_desc} Application
    (Provide one clear best practice, potentially with a brief explanation.)

    ### 4. Liveness and Readiness Probe Configuration for Kubernetes
    (Provide a YAML code block for typical liveness and readiness probes using Markdown fenced code blocks ```yaml ... ```)

    ### 5. How can this {prompt_lang_desc} codebase be instrumented for observability?
    (Provide a concise suggestion for instrumenting the codebase, e.g., mentioning key libraries or approaches.)

    Ensure the overall response is well-structured Markdown.

    """
    

    try:
        logger.info(f"Sending request to OpenAI for app {app_id}. Language: {language}. Prompt length: {len(prompt)} chars.")
        
        # Ensure your OpenAI client (`openai_client`) is initialized correctly.
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", # Or your preferred model like "gpt-3.5-turbo", "gpt-4o"
            messages=[
                {"role": "system", "content": "You are an expert software development and observability assistant providing concise and practical advice."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3, 
            max_tokens=600 # Adjust as needed
        )
        suggestion_text = response.choices[0].message.content.strip()
        model_used = response.model if response.model else "unknown_model"
        
        # individual_suggestions = [s.strip() for s in suggestion_text.split('\n') if s.strip()]
        logger.info(f"Received AI suggestions for app {app_id} using model {model_used}")
        # Your AISuggestionResponse expects `suggestions: str` (a single string)
        # return AISuggestionResponse(app_id=app_id, suggestions=individual_suggestions, model_used=model_used)
        return AISuggestionResponse(app_id=app_id, suggestions=suggestion_text, model_used=model_used)

    except APIConnectionError as e:
        logger.error(f"OpenAI API connection error for app {app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"AI service connection error: {e.__class__.__name__}")
    except RateLimitError as e:
        logger.error(f"OpenAI API rate limit exceeded for app {app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=429, detail="AI service rate limit exceeded. Please try again later.")
    except APIStatusError as e:
        err_detail_msg = str(e)
        status_code_from_api = 500
        if hasattr(e, 'response') and e.response is not None:
            status_code_from_api = e.status_code
            try:
                err_detail_json = e.response.json()
                err_detail_msg = err_detail_json.get("error", {}).get("message", str(e))
            except Exception: # Parsing JSON failed
                pass # Keep default str(e) or use response text
            logger.error(f"OpenAI API status error for app {app_id}: Status {status_code_from_api}, Response: {e.response.text if hasattr(e.response, 'text') else 'N/A'}", exc_info=False)
        else:
             logger.error(f"OpenAI API status error for app {app_id}: {e}", exc_info=True) # No response object, log full exc

        raise HTTPException(status_code=status_code_from_api, detail=f"AI service API error: {err_detail_msg}")
    except Exception as e:
        logger.error(f"Unexpected error during AI analysis for app {app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred during AI analysis: {str(e)}")
