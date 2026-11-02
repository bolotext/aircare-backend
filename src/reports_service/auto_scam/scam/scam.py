import json
import re
import sys
from openai import AzureOpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import random
import os
 


def _load_dotenv(path):
    """Minimal .env loader (no extra dependency). Real environment variables win."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


# Credentials come from the backend's single .env (aircare_backend/.env), never from code.
_BACKEND_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), *([".."] * 4), ".env")
_load_dotenv(os.path.normpath(_BACKEND_ENV))
API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-15-preview")
OPENAI_API = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
llm_model = os.environ.get("SCAM_LLM_MODEL") or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")

# Configuration for parallel processing (override via CLI: python scam.py data.json 100,
# or env SCAM_WORKERS / SCAM_MAX_RETRIES — used by the auto_scam web UI)
MAX_WORKERS = int(os.environ.get("SCAM_WORKERS", 5))
MAX_RETRIES = int(os.environ.get("SCAM_MAX_RETRIES", 3))  # Maximum number of retry attempts
RETRY_DELAY_BASE = 1  # Base delay in seconds for exponential backoff
SAVE_INTERVAL = 200  # Flush output every N completed conversations
PROGRESS_LOG_INTERVAL = 100  # Log progress every N completed conversations

# Default analytics when the conversation has no employee messages (do not call API).
# Single field: free-text English q10 feedback, or not_answered / skipped.
DEFAULT_NO_USER_ANALYTICS = {
    "INTEREST_CLASSIFICATION": "COLD"
}

# Load the Arohan HR q10 analytics prompt from the same directory as this script.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Each path can be overridden by env (the auto_scam web UI sets these per run).
ANALYTICS_PROMPT_YAML = os.environ.get("SCAM_PROMPT_YAML") or os.path.join(SCRIPT_DIR, "analytics_prompt.yaml")
INPUT_DATA_JSON = os.environ.get("SCAM_INPUT_JSON") or os.path.join(SCRIPT_DIR, "mannapura_winback_test.json")
RESULTS_DIR = os.environ.get("SCAM_RESULTS_DIR") or os.path.join(SCRIPT_DIR, "results")
SPLIT_OUTPUT_DIR = os.path.join(SCRIPT_DIR, "split_output")
# Batch mode: process conversations_part_N.json from this part onward (part 1 already done)
DEFAULT_START_PART = 2
PART_FILE_PATTERN = re.compile(r"^conversations_part_(\d+)\.json$")

q10_TRANSCRIPT_PLACEHOLDER = "[INSERT TRANSCRIPT / RESPONSE TEXT HERE]"


def load_analytics_prompt():
    """Load analytics_prompt.yaml; q10 transcript placeholder is replaced at runtime."""
    with open(ANALYTICS_PROMPT_YAML, "r", encoding="utf-8") as f:
        return f.read()


# Load prompt once at startup (used by run_analytics_on_transcription)
prompt = load_analytics_prompt()


def _part_number_from_filename(filename):
    """Extract N from conversations_part_N.json, or None."""
    match = PART_FILE_PATTERN.match(os.path.basename(filename))
    return int(match.group(1)) if match else None


def list_split_input_files(start_part=DEFAULT_START_PART):
    """Return sorted absolute paths for conversations_part_*.json from start_part onward."""
    if not os.path.isdir(SPLIT_OUTPUT_DIR):
        return []

    files = []
    for name in os.listdir(SPLIT_OUTPUT_DIR):
        part_num = _part_number_from_filename(name)
        if part_num is not None and part_num >= start_part:
            files.append((part_num, os.path.join(SPLIT_OUTPUT_DIR, name)))

    files.sort(key=lambda x: x[0])
    return [path for _, path in files]


def output_paths_for_input(input_abs):
    """Result jsonl + legacy json paths under results/ as arohan_hr_analytics.*."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    output_filename = os.path.join(RESULTS_DIR, "winback_analytics.jsonl")
    legacy_output_filename = os.path.join(RESULTS_DIR, "winback_analytics.json")
    return output_filename, legacy_output_filename


def parse_cli_args():
    """
    Returns (input_files, workers).
    - No file arg: batch mode — all split_output parts from DEFAULT_START_PART.
    - File path arg: single file mode.
    - Optional workers: int as sole arg in batch mode, or second arg with file path.
    """
    argv = sys.argv[1:]
    workers = MAX_WORKERS
    input_files = None

    if not argv:
        input_files = list_split_input_files(DEFAULT_START_PART)
        return input_files, workers

    if len(argv) == 1:
        if argv[0].isdigit():
            workers = int(argv[0])
            input_files = list_split_input_files(DEFAULT_START_PART)
        elif os.path.isfile(argv[0]):
            input_files = [os.path.abspath(argv[0])]
        else:
            print(f"Error: file not found: {argv[0]}")
            sys.exit(1)
        return input_files, workers

    input_files = [os.path.abspath(argv[0])]
    workers = int(argv[1])
    return input_files, workers


def _conversation_id(conversation, fallback_index=None):
    """Resolve ID from conversation_id / sender_id / customerCRTId."""
    for key in ("conversation_id", "sender_id", "customerCRTId"):
        value = conversation.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    if fallback_index is not None:
        return f"unknown_{fallback_index}"
    return "unknown"


def format_conversation_for_llm(conversation_log):
    """Format conversation_log as readable 'role: content' lines for the LLM."""
    lines = []
    for msg in conversation_log:
        if isinstance(msg, dict):
            role = msg.get("role", "")
            content = msg.get("content", "")
            lines.append(f"{role}: {content}")
        else:
            lines.append(str(msg))
    return "\n".join(lines) if lines else ""


class ResultsManager:
    """Thread-safe manager for append-only result writing with resume support."""
    
    def __init__(self, output_filename, total_conversations):
        self.output_filename = output_filename
        self.ids_filename = f"{output_filename}.ids"
        self.total_conversations = total_conversations
        self.failed_count = 0
        self.completed_count = 0
        self.processed_ids = set()
        self.lock = threading.Lock()

        # Try to resume from existing results
        self._load_existing_results()

        # Open output files in append mode for fast incremental writes
        self.output_fp = open(self.output_filename, "a", encoding="utf-8")
        self.ids_fp = open(self.ids_filename, "a", encoding="utf-8")
    
    def _load_existing_results(self):
        """Load existing result ids and stats from line-delimited JSON files."""
        try:
            if os.path.exists(self.ids_filename):
                with open(self.ids_filename, "r", encoding="utf-8") as ids_file:
                    for line in ids_file:
                        cid = line.strip()
                        if cid:
                            self.processed_ids.add(cid)

            if os.path.exists(self.output_filename):
                with open(self.output_filename, "r", encoding="utf-8") as file:
                    for line in file:
                        line = line.strip()
                        if not line:
                            continue
                        result = json.loads(line)
                        cid = result.get("customerCRTId")
                        if cid:
                            self.processed_ids.add(cid)
                        if "error" in result.get("analytics", {}) and "Failed after" in result["analytics"]["error"]:
                            self.failed_count += 1

            # Unique IDs only (jsonl may contain duplicate lines from reruns)
            self.completed_count = len(self.processed_ids)

            if self.completed_count:
                print(f"Resumed processing: {self.completed_count} unique conversation IDs already completed")
        except Exception as e:
            print(f"Could not load existing results: {str(e)}")
            self.processed_ids = set()
            self.completed_count = 0
            self.failed_count = 0

    def import_legacy_json_results(self, legacy_filename):
        """Import processed ids from legacy JSON-array output if present."""
        if not os.path.exists(legacy_filename):
            return
        try:
            with open(legacy_filename, "r", encoding="utf-8") as f:
                legacy_results = json.load(f)

            if not isinstance(legacy_results, list):
                return

            imported = 0
            for result in legacy_results:
                customer_id = result.get("customerCRTId")
                if customer_id and customer_id not in self.processed_ids:
                    self.processed_ids.add(customer_id)
                    imported += 1

            if imported > 0:
                print(f"Imported {imported} processed ids from legacy file: {legacy_filename}")
        except Exception as e:
            print(f"Could not import legacy results from {legacy_filename}: {str(e)}")
    
    def add_result(self, result):
        """Add a result and flush periodically."""
        with self.lock:
            customer_id = result.get("customerCRTId")
            is_new_id = bool(customer_id and customer_id not in self.processed_ids)
            if customer_id:
                self.processed_ids.add(customer_id)
                self.ids_fp.write(f"{customer_id}\n")

            self.output_fp.write(json.dumps(result, ensure_ascii=False) + "\n")

            if is_new_id:
                self.completed_count += 1

            # Check if this result indicates a failure
            if "error" in result.get("analytics", {}) and "Failed after" in result["analytics"]["error"]:
                self.failed_count += 1

            # Flush periodically or at the end
            if self.completed_count % SAVE_INTERVAL == 0 or self.completed_count == self.total_conversations:
                self.output_fp.flush()
                self.ids_fp.flush()

            if self.completed_count % PROGRESS_LOG_INTERVAL == 0 or self.completed_count == self.total_conversations:
                print(f"Progress: {self.completed_count}/{self.total_conversations} conversations completed")
    
    def get_stats(self):
        """Get current statistics"""
        with self.lock:
            return {
                'total': self.completed_count,
                'completed': self.completed_count,
                'failed': self.failed_count,
                'successful': self.completed_count - self.failed_count
            }

    def close(self):
        """Close open file handles."""
        with self.lock:
            try:
                self.output_fp.flush()
                self.ids_fp.flush()
            finally:
                self.output_fp.close()
                self.ids_fp.close()
 
def run_analytics_on_transcription(transcription, client, prompt_text=None, model=None, max_retries=None):
    """Run analytics on a single transcription with retry logic.

    prompt_text / model / max_retries override the module defaults (used by the Auto Scam backend)."""
    base_prompt = prompt_text or prompt
    retries = MAX_RETRIES if max_retries is None else max_retries
    for attempt in range(retries + 1):
        try:
            # Convert transcription to string; format as role: content when it's conversation_log
            if isinstance(transcription, list):
                transcription_text = format_conversation_for_llm(transcription)
            else:
                transcription_text = str(transcription)
            
            # Prepare the q10 analytics prompt with the transcription.
            if q10_TRANSCRIPT_PLACEHOLDER in base_prompt:
                formatted_prompt = base_prompt.replace(
                    q10_TRANSCRIPT_PLACEHOLDER,
                    f"<transcription>\n{transcription_text}\n</transcription>",
                    1,
                )
            elif "<transcription>" in base_prompt:
                formatted_prompt = base_prompt.replace(
                    "<transcription>",
                    f"<transcription>\n{transcription_text}\n</transcription>",
                    1,
                )
            else:
                formatted_prompt = f"{base_prompt.strip()}\n\n## Transcription\n{transcription_text}"

            if "json" not in formatted_prompt.lower():
                formatted_prompt += (
                    "\n\nReturn a JSON object with one key INTEREST_CLASSIFICATION: "
                    "one of WARM, COLD, or HOT."
                )

            messages = [{"role": "system", "content": formatted_prompt}]
 
            response = client.chat.completions.create(
                model=model or llm_model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=4096
            )
 
            return response.choices[0].message.content
            
        except Exception as e:
            if attempt < retries:
                # Calculate exponential backoff with jitter
                delay = RETRY_DELAY_BASE * (2 ** attempt) + random.uniform(0, 1)
                print(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {delay:.2f} seconds...")
                time.sleep(delay)
            else:
                print(f"All {retries + 1} attempts failed for transcription. Last error: {str(e)}")
                return json.dumps({"error": f"Failed after {retries + 1} attempts: {str(e)}"})
 
def _has_employee_response(conversation_log):
    """True when at least one user/employee message contains usable text."""
    if not conversation_log:
        return False
    return any(
        (
            isinstance(m, dict)
            and m.get("role") == "user"
            and m.get("content") is not None
            and str(m.get("content", "")).strip()
        )
        for m in conversation_log
    )

def _normalize_analytics(analytics_data, setupTime=None, stage_code=None):
    if not isinstance(analytics_data, dict):
        return analytics_data
    if "error" in analytics_data and "Failed after" in str(analytics_data.get("error", "")):
        return analytics_data
    
    # Keep every output field the prompt returns (a prompt may produce 2-3 outputs, not just the lead score).
    extras = {k: v for k, v in analytics_data.items() if k != "INTEREST_CLASSIFICATION"}
    if "INTEREST_CLASSIFICATION" not in analytics_data and extras:
        return extras  # this prompt has no lead-score output, so the lead-score rules below don't apply

    new_interest_classification = analytics_data.get("INTEREST_CLASSIFICATION")
            
    if stage_code == "INITIAL_MESSAGE_DELIVERED":
        new_interest_classification = "COLD"
        
    if (setupTime or 0) < 35 and stage_code == "INTERESTED" and new_interest_classification == "HOT":
        new_interest_classification = "WARM"

    if stage_code in ["DSCN", "VOICEMAIL", "BUSY", "NO_ANSWER"]:
        new_interest_classification = "COLD"
        
    if not new_interest_classification or new_interest_classification not in ["WARM", "COLD", "HOT"]:
        new_interest_classification = "COLD"
        
    return {"INTEREST_CLASSIFICATION": new_interest_classification, **extras}
    

def make_client():
    """Azure OpenAI client from the environment (aircare_backend/.env). Raises if credentials are missing."""
    key, endpoint = os.environ.get("AZURE_OPENAI_API_KEY"), os.environ.get("AZURE_OPENAI_ENDPOINT")
    if not key or not endpoint:
        raise RuntimeError("Set AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT in aircare_backend/.env")
    return AzureOpenAI(
        api_key=key,
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
        azure_endpoint=endpoint,
    )


def analyze_conversation(conversation, client, prompt_text=None, model=None, max_retries=None, normalize=True):
    """Library entry point (Auto Scam backend): analytics dict for one conversation, or {"error": ...}.

    Same logic as process_conversation, minus the file-based results manager."""
    conversation_log = conversation.get("conversation_log", [])
    if not _has_employee_response(conversation_log):
        analytics_result = json.dumps(DEFAULT_NO_USER_ANALYTICS)
    else:
        analytics_result = run_analytics_on_transcription(conversation_log, client, prompt_text, model, max_retries)
    try:
        data = json.loads(analytics_result)
    except json.JSONDecodeError:
        return {"error": "Failed to parse analytics result"}
    if "error" in data:
        return data
    if not normalize:  # the caller applies its own (UI-configured) rules to the raw model output
        return data
    return _normalize_analytics(data, setupTime=conversation.get("setupTime"), stage_code=conversation.get("STAGE_CODE"))


def process_conversation(conversation_data):
    """Process a single conversation with analytics"""
    conversation, client, index, total, results_manager = conversation_data
    
    customer_crt_id = _conversation_id(conversation, index)
    conversation_log = conversation.get("conversation_log", [])
    
    # No user/employee messages → do not call API; use default (avoids hallucination)
    if not _has_employee_response(conversation_log):
        analytics_result = json.dumps(DEFAULT_NO_USER_ANALYTICS)
    else:
        # Run analytics on the transcription (with built-in retry logic)
        analytics_result = run_analytics_on_transcription(conversation_log, client)
    
    # Parse the analytics result
    try:
        analytics_data = json.loads(analytics_result)
        # Check if the result contains an error from retry attempts
        if "error" in analytics_data and "Failed after" in analytics_data["error"]:
            print(f"Failed to process conversation {index} after all retry attempts")
        else:
            analytics_data = _normalize_analytics(analytics_data, setupTime=conversation.get("setupTime"), stage_code=conversation.get("STAGE_CODE"))
    except json.JSONDecodeError:
        analytics_data = {"error": "Failed to parse analytics result", "raw_result": analytics_result}
        print(f"Failed to parse analytics result for conversation {index}")
    
    # Store the result
    result_entry = {
        "customerCRTId": customer_crt_id,
        "analytics": analytics_data
    }
    
    # Add result to manager (this will save incrementally)
    results_manager.add_result(result_entry)
    
    return result_entry
 
def process_input_file(input_abs, client, workers):
    """Run analytics for one conversations_part_*.json file. Returns True if work was done."""
    output_filename, legacy_output_filename = output_paths_for_input(input_abs)

    try:
        with open(input_abs, "r", encoding="utf-8") as file:
            conversations = json.load(file)
    except FileNotFoundError:
        print(f"Error: {input_abs} not found")
        return False
    except json.JSONDecodeError:
        print(f"Error: Invalid JSON in {input_abs}")
        return False

    if isinstance(conversations, dict):
        conversations = [conversations]
    elif not isinstance(conversations, list):
        print(f"Error: {input_abs} must be a JSON array of conversations")
        return False

    total_conversations = len(conversations)
    part_label = os.path.basename(input_abs)
    print(f"\n{'=' * 72}")
    print(f"File: {part_label}")
    print(f"Input: {input_abs}")
    print(f"Output: {output_filename}")
    print(f"Legacy resume source: {legacy_output_filename}")
    print(f"Processing {total_conversations} conversations in parallel...")

    results_manager = ResultsManager(output_filename, total_conversations)
    results_manager.import_legacy_json_results(legacy_output_filename)
    results_manager.completed_count = len(results_manager.processed_ids)

    processed_ids = results_manager.processed_ids
    conversation_data = []
    for i, conversation in enumerate(conversations, 1):
        customer_id = _conversation_id(conversation, i)
        if customer_id not in processed_ids:
            conversation_data.append((conversation, client, i, total_conversations, results_manager))

    remaining_conversations = len(conversation_data)
    print(f"Remaining conversations to process: {remaining_conversations}")

    if remaining_conversations == 0:
        print(f"Skipping {part_label} — already fully processed ({len(processed_ids)} ids).")
        results_manager.close()
        return False

    max_workers = min(workers, remaining_conversations)
    print(f"Using {max_workers} concurrent workers")
    print(f"Retry configuration: {MAX_RETRIES} max retries with exponential backoff")
    print(f"Flush interval: Every {SAVE_INTERVAL} completed conversations")

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_conversation = {
                executor.submit(process_conversation, data): data
                for data in conversation_data
            }

            for future in as_completed(future_to_conversation):
                try:
                    future.result()
                except Exception as e:
                    conversation_data_item = future_to_conversation[future]
                    conversation_index = conversation_data_item[2]
                    print(f"Error processing conversation {conversation_index}: {str(e)}")
                    error_result = {
                        "customerCRTId": _conversation_id(
                            conversation_data_item[0], conversation_index
                        ),
                        "analytics": {"error": str(e)},
                    }
                    results_manager.add_result(error_result)
    finally:
        results_manager.close()

    stats = results_manager.get_stats()
    print(f"\nCompleted {part_label} → {output_filename}")
    print(f"Processed {stats['total']} conversations total")
    print(f"Successful: {stats['successful']}, Failed: {stats['failed']}")
    return True


def main():
    input_files = [INPUT_DATA_JSON]
    workers = MAX_WORKERS

    if not input_files:
        print(f"No input files found in {SPLIT_OUTPUT_DIR} (start part {DEFAULT_START_PART}+)")
        return

    if not API_KEY or not OPENAI_API:
        print("Error: set AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT in aircare_backend/.env")
        sys.exit(2)

    client = AzureOpenAI(
        api_key=API_KEY,
        api_version=OPENAI_API_VERSION,
        azure_endpoint=OPENAI_API,
    )

    batch_mode = len(input_files) > 1 or (
        len(input_files) == 1
        and SPLIT_OUTPUT_DIR in os.path.abspath(input_files[0])
        and _part_number_from_filename(input_files[0]) is not None
    )

    if batch_mode:
        print(f"Batch mode: {len(input_files)} file(s) from {SPLIT_OUTPUT_DIR}")
        print(f"Starting at part {DEFAULT_START_PART} (part 1 skipped — already done)")
        for path in input_files:
            print(f"  - {os.path.basename(path)}")

    files_processed = 0
    for input_abs in input_files:
        if process_input_file(input_abs, client, workers):
            files_processed += 1

    if batch_mode:
        print(f"\n{'=' * 72}")
        print(f"Batch run finished. {files_processed} file(s) had new work processed.")
    elif not files_processed and input_files:
        print("Nothing new to process for the requested file.")


if __name__ == "__main__":
    main()