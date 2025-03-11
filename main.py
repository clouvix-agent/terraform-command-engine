import os
import json
import shutil
import asyncio
from typing import Dict, Optional
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from dotenv import load_dotenv
import subprocess
from pathlib import Path
import uuid
from collections import deque
from threading import Lock

# Load environment variables
load_dotenv()

app = FastAPI(title="Terraform Execution Engine")
security = HTTPBearer()

# Queue for terraform operations
terraform_queue = deque()
queue_lock = Lock()
current_execution = None

class TerraformRequest(BaseModel):
    terraform_code: str
    variables: Optional[Dict[str, str]] = None

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify the bearer token."""
    expected_token = os.getenv("API_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=500, detail="API token not configured")
    
    if credentials.credentials != expected_token:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return credentials

async def create_terraform_workspace(terraform_code: str, variables: Dict[str, str], workspace_dir: str):
    """Create a temporary workspace with terraform code and variables."""
    os.makedirs(workspace_dir, exist_ok=True)
    
    # Write terraform code
    with open(f"{workspace_dir}/main.tf", "w") as f:
        f.write(terraform_code)
    
    # Write variables if provided
    if variables:
        with open(f"{workspace_dir}/terraform.tfvars", "w") as f:
            for key, value in variables.items():
                f.write(f'{key} = "{value}"\n')

async def execute_terraform_command(command: list, workspace_dir: str) -> Dict:
    """Execute terraform command and return the output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workspace_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        return {
            "success": process.returncode == 0,
            "output": stdout.decode(),
            "error": stderr.decode() if process.returncode != 0 else None
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

async def cleanup_workspace(workspace_dir: str):
    """Clean up the temporary workspace."""
    try:
        shutil.rmtree(workspace_dir)
    except Exception:
        pass

@app.post("/validate")
async def validate_terraform(
    request: TerraformRequest,
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """Validate terraform code."""
    workspace_dir = f"/app/terraform_workspace/{uuid.uuid4()}"
    
    try:
        await create_terraform_workspace(request.terraform_code, request.variables, workspace_dir)
        
        # Initialize terraform
        init_result = await execute_terraform_command(
            ["terraform", "init"],
            workspace_dir
        )
        if not init_result["success"]:
            return {"success": False, "error": init_result["error"]}
        
        # Validate terraform
        validate_result = await execute_terraform_command(
            ["terraform", "validate", "-json"],
            workspace_dir
        )
        
        return {
            "success": validate_result["success"],
            "output": json.loads(validate_result["output"]) if validate_result["success"] else None,
            "error": validate_result["error"]
        }
    
    finally:
        await cleanup_workspace(workspace_dir)

@app.post("/execute")
async def execute_terraform(
    request: TerraformRequest,
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """Execute terraform code."""
    global current_execution
    
    # Generate a unique ID for this execution
    execution_id = str(uuid.uuid4())
    workspace_dir = f"/app/terraform_workspace/{execution_id}"
    
    with queue_lock:
        # Add to queue if there's already an execution in progress
        if current_execution:
            terraform_queue.append((execution_id, request))
            return {
                "status": "queued",
                "position": len(terraform_queue),
                "execution_id": execution_id
            }
        current_execution = execution_id
    
    try:
        await create_terraform_workspace(request.terraform_code, request.variables, workspace_dir)
        
        # Initialize terraform
        init_result = await execute_terraform_command(
            ["terraform", "init"],
            workspace_dir
        )
        if not init_result["success"]:
            return {"success": False, "error": init_result["error"]}
        
        # Plan terraform
        plan_result = await execute_terraform_command(
            ["terraform", "plan"],
            workspace_dir
        )
        if not plan_result["success"]:
            return {"success": False, "error": plan_result["error"]}
        
        # Apply terraform
        apply_result = await execute_terraform_command(
            ["terraform", "apply", "-auto-approve"],
            workspace_dir
        )
        
        return {
            "success": apply_result["success"],
            "output": apply_result["output"],
            "error": apply_result["error"]
        }
    
    finally:
        await cleanup_workspace(workspace_dir)
        with queue_lock:
            current_execution = None
            # Process next item in queue if any
            if terraform_queue:
                next_id, next_request = terraform_queue.popleft()
                asyncio.create_task(execute_terraform(next_request, credentials))

@app.get("/queue-status")
async def get_queue_status(
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """Get the current status of the terraform execution queue."""
    return {
        "current_execution": current_execution,
        "queue_length": len(terraform_queue)
    } 