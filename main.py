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

load_dotenv()

app = FastAPI(title="Terraform Execution Engine")
security = HTTPBearer()

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
    """Create a temporary workspace with terraform file and variables for AWS, GCP, and Azure."""
    
    os.makedirs(workspace_dir, exist_ok=True)
    
    file_content = await terraform_file.read()
    file_content_str = file_content.decode('utf-8')
    
    if variables:
        # Extract AWS Credentials
        aws_credentials = {}
        for key in ['aws_access_key', 'aws_secret_key', 'aws_region']:
            if key in variables:
                aws_credentials[key] = variables.pop(key)

        # Extract GCP Credentials
        gcp_credentials = {}
        if "gcp_credentials_file" in variables:
            gcp_credentials["credentials_file"] = variables.pop("gcp_credentials_file")

        # Extract Azure Credentials
        azure_credentials = {}
        for key in ["azure_client_id", "azure_client_secret", "azure_subscription_id", "azure_tenant_id"]:
            if key in variables:
                azure_credentials[key] = variables.pop(key)

        # Inject AWS Provider Block
        if aws_credentials:
            provider_block = 'provider "aws" {\n'
            if "aws_access_key" in aws_credentials:
                provider_block += f'  access_key = "{aws_credentials["aws_access_key"]}"\n'
            if "aws_secret_key" in aws_credentials:
                provider_block += f'  secret_key = "{aws_credentials["aws_secret_key"]}"\n'
            if "aws_region" in aws_credentials:
                provider_block += f'  region     = "{aws_credentials["aws_region"]}"\n'
            provider_block += '}\n'

            # Remove any existing AWS provider blocks
            lines = file_content_str.split('\n')
            new_lines = []
            skip = False
            for line in lines:
                if 'provider "aws"' in line:
                    skip = True
                    continue
                if skip and '}' in line:
                    skip = False
                    continue
                if not skip:
                    new_lines.append(line)
            
            file_content_str = '\n'.join(new_lines)
            file_content_str = provider_block + file_content_str

        # Inject GCP Provider Block
        if gcp_credentials:
            provider_block = 'provider "google" {\n'
            if "credentials_file" in gcp_credentials:
                provider_block += f'  credentials = file("{gcp_credentials["credentials_file"]}")\n'
            provider_block += '}'

            if 'provider "google"' in file_content_str:
                file_content_str = file_content_str.replace('provider "google" {}', provider_block)
                file_content_str = file_content_str.replace('provider "google" {', provider_block)
            else:
                file_content_str += f"\n{provider_block}\n"

        # Inject Azure Provider Block
        if azure_credentials:
            provider_block = 'provider "azurerm" {\n'
            provider_block += '  features {}\n' 
            if "azure_client_id" in azure_credentials:
                provider_block += f'  client_id       = "{azure_credentials["azure_client_id"]}"\n'
            if "azure_client_secret" in azure_credentials:
                provider_block += f'  client_secret   = "{azure_credentials["azure_client_secret"]}"\n'
            if "azure_subscription_id" in azure_credentials:
                provider_block += f'  subscription_id = "{azure_credentials["azure_subscription_id"]}"\n'
            if "azure_tenant_id" in azure_credentials:
                provider_block += f'  tenant_id       = "{azure_credentials["azure_tenant_id"]}"\n'
            provider_block += '}'

            if 'provider "azurerm"' in file_content_str:
                file_content_str = file_content_str.replace('provider "azurerm" {}', provider_block)
                file_content_str = file_content_str.replace('provider "azurerm" {', provider_block)
            else:
                file_content_str += f"\n{provider_block}\n"

    # Save the modified Terraform file
    with open(f"{workspace_dir}/main.tf", "w") as f:
        f.write(file_content_str)
    
    # Save other variables to terraform.tfvars
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
    """Validate terraform code and run plan, apply, and destroy if validation succeeds."""
    workspace_dir = f"/app/terraform_workspace/{uuid.uuid4()}"
    
    try:
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
        
        if not validate_result["success"]:
            return {
                "success": False,
                "error": validate_result["error"],
                "details": {
                    "type": "validation_error",
                    "message": "Terraform configuration validation failed"
                }
            }
            
        # Plan terraform
        plan_result = await execute_terraform_command(
            ["terraform", "plan"],
            workspace_dir
        )
        if not plan_result["success"]:
            return {
                "success": False,
                "error": plan_result["error"],
                "details": {
                    "type": "plan_error",
                    "message": "Terraform plan failed"
                }
            }
            
        # Apply terraform
        apply_result = await execute_terraform_command(
            ["terraform", "apply", "-auto-approve"],
            workspace_dir
        )
        if not apply_result["success"]:
            return {
                "success": False,
                "error": apply_result["error"],
                "details": {
                    "type": "apply_error",
                    "message": "Terraform apply failed"
                }
            }
            
        # Destroy terraform
        destroy_result = await execute_terraform_command(
            ["terraform", "destroy", "-auto-approve"],
            workspace_dir
        )
        if not destroy_result["success"]:
            return {
                "success": False,
                "error": destroy_result["error"],
                "details": {
                    "type": "destroy_error",
                    "message": "Terraform destroy failed"
                }
            }
            
        # If all steps succeeded, return success with all outputs
        return {
            "success": True,
            "validation": json.loads(validate_result["output"]),
            "plan": plan_result["output"],
            "apply": apply_result["output"],
            "destroy": destroy_result["output"]
        }
            
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format for variables")
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "details": {
                "type": "unexpected_error",
                "message": "An unexpected error occurred during execution"
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
        parsed_variables = json.loads(variables) if variables else None
        execution_id = str(uuid.uuid4())
        workspace_dir = f"/app/terraform_workspace/{execution_id}"
        
        with queue_lock:
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
            
            init_result = await execute_terraform_command(
                ["terraform", "init"],
                workspace_dir
            )
            if not init_result["success"]:
                return {"success": False, "error": init_result["error"]}
            
            plan_result = await execute_terraform_command(
                ["terraform", "plan"],
                workspace_dir
            )
            if not plan_result["success"]:
                return {"success": False, "error": plan_result["error"]}
            
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