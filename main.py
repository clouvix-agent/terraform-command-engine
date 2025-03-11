import os
import json
import shutil
import asyncio
from typing import Dict, Optional
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
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
    variables: Optional[Dict[str, str]] = None

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify the bearer token."""
    expected_token = os.getenv("API_TOKEN")
    if not expected_token:
        raise HTTPException(status_code=500, detail="API token not configured")
    
    if credentials.credentials != expected_token:
        raise HTTPException(status_code=401, detail="Invalid authentication token")
    return credentials

async def create_terraform_workspace(terraform_file: UploadFile, variables: Dict[str, str], workspace_dir: str):
    """Create a temporary workspace with terraform file and variables."""
    os.makedirs(workspace_dir, exist_ok=True)
    
    # Write terraform file
    file_content = await terraform_file.read()
    with open(f"{workspace_dir}/main.tf", "wb") as f:
        f.write(file_content)
    
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
    terraform_file: UploadFile = File(...),
    variables: str = Form(None),
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """Validate terraform code."""
    workspace_dir = f"/app/terraform_workspace/{uuid.uuid4()}"
    
    try:
        # Parse variables from JSON string if provided
        parsed_variables = json.loads(variables) if variables else None
        
        await create_terraform_workspace(terraform_file, parsed_variables, workspace_dir)
        
        # Initialize terraform
        init_result = await execute_terraform_command(
            ["terraform", "init"],
            workspace_dir
        )
        if not init_result["success"]:
            return {
                "success": False,
                "error": init_result["error"],
                "details": {
                    "type": "initialization_error",
                    "message": "Failed to initialize terraform workspace"
                }
            }
        
        # Validate terraform
        validate_result = await execute_terraform_command(
            ["terraform", "validate", "-json"],
            workspace_dir
        )
        
        if validate_result["success"]:
            return {
                "success": True,
                "output": json.loads(validate_result["output"])
            }
        else:
            return {
                "success": False,
                "error": validate_result["error"],
                "details": {
                    "type": "validation_error",
                    "message": "Terraform configuration validation failed"
                }
            }
            
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format for variables")
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "details": {
                "type": "unexpected_error",
                "message": "An unexpected error occurred during validation"
            }
        }
    finally:
        await cleanup_workspace(workspace_dir)

@app.post("/execute")
async def execute_terraform(
    terraform_file: UploadFile = File(...),
    variables: str = Form(None),
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """Execute terraform code."""
    global current_execution
    
    try:
        # Parse variables from JSON string if provided
        parsed_variables = json.loads(variables) if variables else None
        
        # Generate a unique ID for this execution
        execution_id = str(uuid.uuid4())
        workspace_dir = f"/app/terraform_workspace/{execution_id}"
        
        with queue_lock:
            # Add to queue if there's already an execution in progress
            if current_execution:
                terraform_queue.append((execution_id, terraform_file, parsed_variables))
                return {
                    "status": "queued",
                    "position": len(terraform_queue),
                    "execution_id": execution_id
                }
            current_execution = execution_id
        
        try:
            await create_terraform_workspace(terraform_file, parsed_variables, workspace_dir)
            
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
                    next_id, next_file, next_vars = terraform_queue.popleft()
                    asyncio.create_task(execute_terraform(next_file, next_vars, credentials))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format for variables")

@app.get("/queue-status")
async def get_queue_status(
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """Get the current status of the terraform execution queue."""
    return {
        "current_execution": current_execution,
        "queue_length": len(terraform_queue)
    } 