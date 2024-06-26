import gc
import time
import requests
from typing import Optional
import os
import multiprocessing
import argparse
from pydantic import BaseModel

import pandas as pd
import uvicorn
import torch
from fastapi import FastAPI, HTTPException
from fastapi.logger import logger
from supabase import create_client

logger.setLevel("INFO")

from utilities.validation_utils import regenerate_hash, check_model_repo_size, get_model_size

from dotenv import load_dotenv

load_dotenv()

# Constants
MAX_GENERATION_LEEWAY = 0.5 # should be between 0 and 1. This is the percentage of tokens that the model can generate more than the last user message
MAX_GENERATION_LENGTH = 200 # maximum number of tokens that the model can generate
LENGTH_DIFF_PENALTY_STEEPNESS = 2 # the steepness of the exponential decay of the length difference penalty
QUALITATIVE_SCORE_WEIGHT = 0.82 # weight of the qualitative score in the total score
MODEL_SIZE_SCORE_WEIGHT = 0.06 # weight of the model size score in the total score
LATENCY_SCORE_WEIGHT = 0.06 # weight of the latency score in the total score
VIBE_SCORE_WEIGHT = 0.06 # weight of the vibe score in the total score
MAX_AVG_LATENCY = 10000 # in milliseconds

MAX_MODEL_SIZE = 30 * 1024 * 1024 * 1024 # in bytes
MIN_REPO_SIZE = 10 * 1024 * 1024 # in bytes
MAX_REPO_SIZE = 80 * 1024 * 1024 * 1024 #  in bytes
SAMPLE_SIZE = 1024 # number of samples to evaluate the model from the dataset
BATCH_SIZE = 4 # batch size for evaluation
VOCAB_TRUNCATION = 1000 # truncate the vocab to top n tokens
PROB_TOP_K = 10 # the correct token should be in the top n tokens, else a score of 0 is given to that token
# TODO: this will truncate the sequence to MAX_SEQ_LEN tokens. This is a temporary fix to make the evaluation faster.
MAX_SEQ_LEN = 4096 # maximum sequence length that should be allowed because eval gets really slow with longer sequences than this

MAX_SEQ_LEN_VIBE_SCORE = 2048 # maximum sequence length that should be allowed for vibe score calculation because it is slow with longer sequences than this
BATCH_SIZE_VIBE_SCORE = 4 # batch size for vibe score calculation
SAMPLE_SIZE_VIBE_SCORE = 128 # number of samples to evaluate the model from the dataset for vibe score calculation

SAVE_LEADERBOARD_EVERY = 60 # save the leaderboard every 60 seconds

EVAL_SCORE_PORT = 8001 # default port for the eval_score API
VIBE_SCORE_PORT = 8002 # default port for the vibe_score API


leaderboard_file = 'leaderboard.csv'

class EvaluateModelRequest(BaseModel):
    repo_namespace: str
    repo_name: str
    chat_template_type: str
    hash: str
    revision: Optional[str] = "main"
    competition_id: Optional[str] = "d1"

app = FastAPI()

chat_template_mappings = {
    "vicuna": "prompt_templates/vicuna_prompt_template.jinja",
    "chatml": "prompt_templates/chatml_prompt_template.jinja",
    "mistral": "prompt_templates/mistral_prompt_template.jinja",
    "zephyr": "prompt_templates/zephyr_prompt_template.jinja",
    "alpaca": "prompt_templates/alpaca_prompt_template.jinja",
    "llama2": "prompt_templates/llama2_prompt_template.jinja",
    "llama3": "prompt_templates/llama3_prompt_template.jinja",
}

class ThreadSafeLeaderboardManager:
    def __init__(self, namespace):
        self.namespace = namespace
        self.lock = multiprocessing.Lock()

    def __enter__(self):
        self.lock.acquire()
        return self.get_leaderboard()

    def __exit__(self, exc_type, exc_value, traceback):
        self.lock.release()

    def get_leaderboard(self):
        if not hasattr(self.namespace, 'leaderboard'):
            dtype_dict = {
                'hash': str,
                'repo_namespace': str,
                'repo_name': str,
                'chat_template_type': str,
                'model_size_score': 'float64',  # Use 'float64' to allow NaNs
                'qualitative_score': 'float64',  # Use 'float64' to allow NaNs
                'latency_score': 'float64',  # Use 'float64' to allow NaNs
                'vibe_score': 'float64',  # Use 'float64' to allow NaNs
                'total_score': 'float64',  # Use 'float64' to allow NaNs
                'timestamp': str,
                'status': str,
                'notes': str
            }
            leaderboard = pd.read_csv(leaderboard_file, dtype=dtype_dict, parse_dates=['timestamp'])
            # Replace NaN with None for JSON serialization
            leaderboard = leaderboard.where(pd.notnull(leaderboard), None)
            self.namespace.leaderboard = leaderboard
        
        return self.namespace.leaderboard

# Usage example:
# with ThreadSafeLeaderboardManager(namespace) as leaderboard:
#     # Perform thread-safe operations on leaderboard

def save_leaderboard(leaderboard: pd.DataFrame):
    leaderboard.to_csv(leaderboard_file, index=False)

def model_evaluation_worker(evaluation_queue, namespace):
    while True:
        request = evaluation_queue.get()
        if request is None:  # Sentinel value to exit the process
            break
        try:
            with torch.no_grad():  # Disable gradient calculation
                result = evaluate_model_logic(request, namespace)
                logger.info(f"Model evaluation completed: {result}")
        except Exception as e:
            logger.error(f"Error during model evaluation: {e}")
        finally:
            gc.collect()  # Garbage collect
            if torch.cuda.is_available():
                torch.cuda.empty_cache()  # Empty CUDA cache

        
def evaluate_model_logic(request: EvaluateModelRequest, namespace):
    """
    Evaluate a model based on the model size and the quality of the model.
    """
    with ThreadSafeLeaderboardManager(namespace) as leaderboard:
        if not (leaderboard['hash'] == request.hash).any():
            logger.debug(leaderboard)
            logger.debug(leaderboard['hash'])
            logger.debug(type(leaderboard['hash']))
            logger.debug(request.hash)
            logger.debug(type(request.hash))
            raise ValueError(f"Model {request.hash} not found in the leaderboard")
        
        # changed status to in progress
        update_leaderboard_status(namespace, request.hash, "RUNNING", "Model evaluation in progress")
    
    logger.info("Model evaluation in progress")
    start_time = time.time()
    eval_score_response = None
    while True:
        try:
            eval_score_response = requests.post(f"http://localhost:{EVAL_SCORE_PORT}/eval_score", json=request.model_dump())
            if eval_score_response.status_code == 200:
                logger.info("eval_score API call successful")
                break
            else:
                raise RuntimeError(f"Error calling eval_score API: {eval_score_response.content}")
        except Exception as e:
            if time.time() - start_time > 30:
                error_string = f"Error calling eval_score API with message: {eval_score_response.content if eval_score_response else e}"
                
                with ThreadSafeLeaderboardManager(namespace) as leaderboard:
                    update_leaderboard_status(namespace, request.hash, "FAILED", error_string)
                
                try:
                    shutdown_response = requests.post(f"http://localhost:{EVAL_SCORE_PORT}/shutdown", timeout=1)
                except Exception as shutdown_error:
                    pass
                
                raise RuntimeError(error_string)
        
        time.sleep(1)  # Wait for 1 second before retrying
    
    # Call the shutdown endpoint to restart the eval_score_api for the next evaluation to avoid memory leaks that were observed with loading and unloading different models
    logger.info("Shutting down eval_score_api")
    try:
        shutdown_response = requests.post(f"http://localhost:{EVAL_SCORE_PORT}/shutdown", timeout=1)
    except Exception as e:
        pass
    logger.info("vibe_score_api shutdown initiated for restart.")
    
    eval_score_data = eval_score_response.json()
    eval_score = eval_score_data["eval_score"]
    latency_score = eval_score_data["latency_score"]
    model_size_score = eval_score_data["model_size_score"]

    # update the leaderboard with only the scores that are available and update the notes
    with ThreadSafeLeaderboardManager(namespace) as leaderboard:
        leaderboard.loc[leaderboard['hash'] == request.hash, 'model_size_score'] = float(model_size_score)
        leaderboard.loc[leaderboard['hash'] == request.hash, 'qualitative_score'] = float(eval_score)
        leaderboard.loc[leaderboard['hash'] == request.hash, 'latency_score'] = float(latency_score)
        leaderboard.loc[leaderboard['hash'] == request.hash, 'notes'] = "Now computing vibe score"
        namespace.leaderboard = leaderboard
        save_leaderboard(namespace.leaderboard)

    # Call the vibe_score API
    start_time = time.time()
    vibe_score_response = None
    while True:
        try:
            vibe_score_response = requests.post(f"http://localhost:{VIBE_SCORE_PORT}/vibe_match_score", json=request.model_dump())
            if vibe_score_response.status_code == 200:
                logger.info("vibe_score API call successful")
                break
            else:
                raise RuntimeError(f"Error calling vibe_score API: {vibe_score_response.content}")
        except Exception as e:
            if time.time() - start_time > 30:
                error_string = f"Error calling vibe_score API with message: {vibe_score_response.content if vibe_score_response else e}"
                with ThreadSafeLeaderboardManager(namespace):
                    update_leaderboard_status(namespace, request.hash, "FAILED", error_string)
                
                try:
                    shutdown_response = requests.post(f"http://localhost:{VIBE_SCORE_PORT}/shutdown", timeout=1)
                except Exception as shutdown_error:
                    logger.error(f"Error during vibe_score_api shutdown: {shutdown_error}")
                
                raise RuntimeError(error_string)
        
        time.sleep(1)  # Wait for 1 second before retrying
    
    # Call the shutdown endpoint to restart the vibe_score_api for the next evaluation to avoid memory leaks that were observed with loading and unloading different models
    logger.info("Shutting down vibe_score_api")
    try:
        shutdown_response = requests.post(f"http://localhost:{VIBE_SCORE_PORT}/shutdown", timeout=1)
    except Exception as e:
        pass

    vibe_score = vibe_score_response.json()["vibe_score"]

    if eval_score is None or latency_score is None or model_size_score is None or vibe_score is None:
        raise HTTPException(status_code=500, detail="Error calculating scores, one or more scores are None")
    
    total_score = model_size_score * MODEL_SIZE_SCORE_WEIGHT
    total_score += eval_score * QUALITATIVE_SCORE_WEIGHT
    total_score += latency_score * LATENCY_SCORE_WEIGHT
    total_score += vibe_score * VIBE_SCORE_WEIGHT

    try:
        with ThreadSafeLeaderboardManager(namespace) as leaderboard:
            logger.info("Updating leaderboard to COMPLETED")
            leaderboard.loc[leaderboard['hash'] == request.hash, 'model_size_score'] = float(model_size_score)
            leaderboard.loc[leaderboard['hash'] == request.hash, 'qualitative_score'] = float(eval_score)
            leaderboard.loc[leaderboard['hash'] == request.hash, 'latency_score'] = float(latency_score)
            leaderboard.loc[leaderboard['hash'] == request.hash, 'vibe_score'] = float(vibe_score)
            leaderboard.loc[leaderboard['hash'] == request.hash, 'total_score'] = float(total_score)
            leaderboard.loc[leaderboard['hash'] == request.hash, 'status'] = "COMPLETED"
            leaderboard.loc[leaderboard['hash'] == request.hash, 'notes'] = ""
            namespace.leaderboard = leaderboard
        
    except Exception as e:
        failure_reason = str(e)
        with ThreadSafeLeaderboardManager(namespace) as leaderboard:
            logger.error(f"Updating leaderboard to FAILED: {failure_reason}")
            update_leaderboard_status(request.hash, "FAILED", failure_reason)
        
        raise RuntimeError("Error updating leaderboard: " + failure_reason)
    
    return {
        "model_size_score": model_size_score,
        "qualitative_score": eval_score,
        "latency_score": latency_score,
        "vibe_score": vibe_score,
        "total_score": total_score
    }


def update_leaderboard_status(namespace, hash, status, notes=""):
    leaderboard = namespace.leaderboard
    try:
        leaderboard.loc[leaderboard['hash'] == hash, 'status'] = status
        if notes:
            leaderboard.loc[leaderboard['hash'] == hash, 'notes'] = notes

        namespace.leaderboard = leaderboard
        save_leaderboard(namespace.leaderboard)
    except Exception as e:
        logger.error(f"Error updating leaderboard status for {hash}: {e}")


def get_json_result(namespace, hash):
    leaderboard = namespace.leaderboard
    if (leaderboard['hash'] == hash).any():
        # if it exists, return score and status
        model_entry = leaderboard[leaderboard['hash'] == hash].iloc[0]
        
        return {
            "score": {
                "model_size_score": model_entry['model_size_score'],
                "qualitative_score": model_entry['qualitative_score'],
                "latency_score": model_entry['latency_score'],
                "vibe_score": model_entry['vibe_score'],
                "total_score": model_entry['total_score']
            },
            "status": model_entry['status']
        }
    else:
        None


@app.post("/evaluate_model")
def evaluate_model(request: EvaluateModelRequest):
    # verify hash
    if int(request.hash) != regenerate_hash(request.repo_namespace, request.repo_name, request.chat_template_type, request.competition_id):
        raise HTTPException(status_code=400, detail="Hash does not match the model details")

    # check if the model already exists in the leaderboard
    with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
        # This needs to be a virtually atomic operation
        current_status = get_json_result(app.state.ns, request.hash)
        if current_status is None:
            failure_notes = ""
            # add the model to leaderboard with status QUEUED
            new_entry = pd.DataFrame([{
                "hash": request.hash,
                "repo_namespace": request.repo_namespace,
                "repo_name": request.repo_name,
                "chat_template_type": request.chat_template_type,
                "model_size_score": -1.0,
                "qualitative_score": -1.0,
                "latency_score": -1.0,
                "vibe_score": -1.0,
                "total_score": -1.0,
                "timestamp": pd.Timestamp.utcnow(),
                "status": "QUEUED",
                "notes": ""
            }])

            app.state.ns.leaderboard = pd.concat([new_entry, leaderboard], ignore_index=True)
            save_leaderboard(app.state.ns.leaderboard)
    
    if current_status is not None:
        return current_status

    # validate the request
    if request.chat_template_type not in chat_template_mappings:
        failure_notes = f"Chat template type not supported: {request.chat_template_type}"
        logger.error(failure_notes)
        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            update_leaderboard_status(app.state.ns, request.hash, "FAILED", failure_notes)
            return get_json_result(app.state.ns, request.hash)

    # check repo size of the model to see if it is within the limit
    try:
        model_repo_size = check_model_repo_size(request.hash, request.repo_namespace, request.repo_name)
        if model_repo_size is None:
            failure_notes = "Error checking model repo size. Make sure the model repository exists and is accessible."
            logger.error(failure_notes)
            with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
                update_leaderboard_status(app.state.ns, request.hash, "FAILED", failure_notes)
                return get_json_result(app.state.ns, request.hash)
    
    except Exception as e:
        failure_notes = f"Error checking model repo size: {e}"
        logger.error(failure_notes)
        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            update_leaderboard_status(app.state.ns, request.hash, "FAILED", failure_notes)
            return get_json_result(app.state.ns, request.hash)


    if model_repo_size > MAX_REPO_SIZE or model_repo_size < MIN_REPO_SIZE:
        failure_notes = f"Model repo size is not up to requirments: {model_repo_size} bytes. Should be less than {MAX_REPO_SIZE} bytes and greater than {MIN_REPO_SIZE} bytes"
        logger.error(failure_notes)
        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            update_leaderboard_status(app.state.ns, request.hash, "FAILED", failure_notes)
            return get_json_result(app.state.ns, request.hash)

    # check model size by checking safetensors index
    model_size = get_model_size(request.repo_namespace, request.repo_name)
    if model_size is None:
        failure_notes = "Error getting model size. Make sure the model.index.safetensors.json file exists in the model repository. And it has the metadata->total_size field."
        logger.error(failure_notes)
        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            update_leaderboard_status(app.state.ns, request.hash, "FAILED", failure_notes)
            return get_json_result(app.state.ns, request.hash)

    if (model_size // 4) > MAX_MODEL_SIZE:
        failure_notes = f"Model size is too large: {model_size} bytes. Should be less than {MAX_MODEL_SIZE} bytes"
        logger.error(failure_notes)
        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            update_leaderboard_status(app.state.ns, request.hash, "FAILED", failure_notes)
            return get_json_result(app.state.ns, request.hash)

    # Add the evaluation task to the queue
    evaluation_queue.put(request)

    logger.info('returning result')
    with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
        return get_json_result(app.state.ns, request.hash)


def save_leaderboard_periodically(periodic_save_event, namespace):
    while not periodic_save_event.is_set():
        with ThreadSafeLeaderboardManager(namespace) as leaderboard:
            logger.info("Saving leaderboard")
            save_leaderboard(leaderboard)
        
        periodic_save_event.wait(SAVE_LEADERBOARD_EVERY)
    

@app.get("/leaderboard")
def display_leaderboard():
    with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
        return leaderboard.to_dict(orient='records')

if __name__ == "__main__":
    # add command line arguments for the ports of the two apis
    import argparse
    parser = argparse.ArgumentParser(description="Run the server")
    parser.add_argument("--main-api-port", type=int, default=8000, help="Port for the main API")
    parser.add_argument("--eval-score-port", type=int, default=8001, help="Port for the eval_score API")
    parser.add_argument("--vibe-score-port", type=int, default=8002, help="Port for the vibe_score API")
    parser.add_argument("--save-remote", action="store_true", default=False, help="Enable remote saving")
    args = parser.parse_args()

    MAIN_API_PORT = args.main_api_port
    EVAL_SCORE_PORT = args.eval_score_port
    VIBE_SCORE_PORT = args.vibe_score_port
    # try:
    #     multiprocessing.set_start_method('spawn', force=True) # need to fo
    # except RuntimeError as e:
    #     logger.warning(f"Warning: multiprocessing context has already been set. Details: {e}")
    
    evaluation_queue = multiprocessing.Queue()

    SAVE_REMOTE = args.save_remote

    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    try:
        supabase_client = create_client(supabase_url, supabase_key)
    except Exception as e:
        logger.warning(f"Failed to create Supabase client: {e}. Leaderboard will only be saved locally.")
        supabase_client = None
        SAVE_REMOTE = False

    
    # Create a global shared namespace for the leaderboard
    manager_instance = multiprocessing.Manager()
    app.state.ns = manager_instance.Namespace()

    # if the leaderboard file does not exist, create it with proper columns
    columns = ['hash', 'repo_namespace', 'repo_name', 'chat_template_type', 'model_size_score', 'qualitative_score', 'latency_score', 'vibe_score', 'total_score', 'timestamp', 'status', 'notes']
    if not os.path.exists(leaderboard_file):
        # fetch from supabase
        try:
            leaderboard = pd.DataFrame(columns=columns)
            leaderboard.to_csv(leaderboard_file, index=False)
        
        except Exception as e:
            logger.error(f"Error fetching leaderboard from Supabase: {e}")
            leaderboard = pd.DataFrame(columns=columns)
            leaderboard.to_csv(leaderboard_file, index=False)
    else:
        # if the file exists, load it
        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            # check if the leaderboard has the correct columns
            if not all(col in leaderboard.columns for col in columns):
                logger.error(f"Leaderboard file does not have the correct columns. Creating a new leaderboard file...")
                # quit
                import sys
                sys.exit(1)

    try:
        logger.info("Starting evaluation thread")
        evaluation_process = multiprocessing.Process(target=model_evaluation_worker, args=(evaluation_queue, app.state.ns))
        periodic_save_event = multiprocessing.Event()
        periodic_save_process = multiprocessing.Process(target=save_leaderboard_periodically, args=(periodic_save_event, app.state.ns))
        periodic_save_process.start()
        evaluation_process.start()
        logger.info("Starting API server")
        uvicorn.run(app, host="0.0.0.0", port=MAIN_API_PORT)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, stopping...")
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
    finally:
        logger.info("Stopping evaluation thread")
        # empty the queue
        while not evaluation_queue.empty():
            evaluation_queue.get()
        
        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            # remove any rows with status QUEUED
            app.state.ns.leaderboard = leaderboard[leaderboard['status'] != 'QUEUED']
            save_leaderboard(app.state.ns.leaderboard)

        # stop the periodic save process
        periodic_save_event.set()

        # add a sentinel to the queue to stop the thread
        evaluation_queue.put(None)

        # wait for the evaluation thread to finish
        evaluation_process.join()
        # wait for the periodic save process to finish
        periodic_save_process.join()

        with ThreadSafeLeaderboardManager(app.state.ns) as leaderboard:
            # remove any RUNNING status
            app.state.ns.leaderboard = leaderboard[leaderboard['status'] != 'RUNNING']
            save_leaderboard(app.state.ns.leaderboard)
            logger.info("API server and evaluation thread have been stopped")
