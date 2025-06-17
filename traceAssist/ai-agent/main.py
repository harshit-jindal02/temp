from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
# import openai # This line is redundant if you're using "from openai import OpenAI"
from openai import OpenAI, APIConnectionError, RateLimitError, APIStatusError # Good to import specific errors
import os
import logging # Recommended for better error insights

# Configure logging (simple example)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- OpenAI Client Initialization (Robust Version) ---
openai_api_key_from_env = os.getenv("OPENAI_API_KEY")
client: OpenAI | None = None # Use | for Union type hint in Python 3.10+ or from typing import Optional

if openai_api_key_from_env:
    cleaned_api_key = openai_api_key_from_env.strip()
    if cleaned_api_key:
        try:
            client = OpenAI(api_key=cleaned_api_key)
            logger.info("OpenAI client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
    else:
        logger.error("OPENAI_API_KEY environment variable is set but contains only whitespace.")
else:
    logger.warning("OPENAI_API_KEY environment variable not found. AI features will be disabled or raise errors.")
# --- End OpenAI Client Initialization ---


app = FastAPI()

# ─── CORS ───────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000"], # Consider using ["*"] for dev or more specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# ────────────────────────────────────────────────────────────────────────────────

class SuggestRequest(BaseModel):
    app_id: str
    # add other fields (code snippet, files) as needed

class SuggestResponse(BaseModel):
    suggestions: str # This expects a list of strings
    model_used: str
    app_id: str # Good to echo back the app_id

@app.post("/suggestions", response_model=SuggestResponse) # Added response_model
async def suggest(req: SuggestRequest):
    if not client:
        logger.error("OpenAI client not configured. Cannot process /suggestions request.")
        raise HTTPException(status_code=503, detail="AI service unavailable: OpenAI client not configured.")

    try:
        logger.info(f"Requesting suggestions for app_id: {req.app_id}")
        completion = client.chat.completions.create( # Renamed to 'completion' for clarity
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an observability expert."},
                {"role": "user", "content": f"Instrument code improvements for app {req.app_id}"}
            ],
            # n=1 is default, but if you change it, this code will still work
        )

        # Correctly extract all suggestions into a list
        suggestions_list = [choice.message.content for choice in completion.choices if choice.message and choice.message.content]
        
        model_identifier = completion.model # The model string returned by the API

        logger.info(f"Received {len(suggestions_list)} suggestion(s) for app_id: {req.app_id} using model: {model_identifier}")

        return SuggestResponse(
            suggestions=suggestions_list,
            model_used=model_identifier,
            app_id=req.app_id # Echoing back the app_id
        )

    # More specific error handling (recommended)
    except APIConnectionError as e:
        logger.error(f"OpenAI API connection error for app {req.app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"AI service connection error: {e.__class__.__name__}")
    except RateLimitError as e:
        logger.error(f"OpenAI API rate limit exceeded for app {req.app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=429, detail="AI service rate limit exceeded. Please try again later.")
    except APIStatusError as e:
        err_detail_msg = str(e)
        try:
            if e.response and e.response.content:
                err_detail_json = e.response.json()
                err_detail_msg = err_detail_json.get("error", {}).get("message", str(e))
        except Exception:
            pass # Keep default str(e)
        logger.error(f"OpenAI API status error for app {req.app_id}: Status {e.status_code}, Response: {e.response.text if e.response else 'N/A'}", exc_info=False)
        raise HTTPException(status_code=e.status_code if e.status_code else 500, detail=f"AI service API error: {err_detail_msg}")
    except Exception as e:
        logger.error(f"Unexpected error during AI suggestion for app {req.app_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {str(e)}")