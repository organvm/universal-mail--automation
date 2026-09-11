import os
import json
import base64
import logging
from typing import Optional, Dict, Any
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

class VaultSync:
    """
    Synchronizes state to a centralized 'Vault' repository via the GitHub API.
    This allows organizational repos to remain stateless, keeping private data
    in a dedicated, secure personal repository.
    """
    def __init__(self, repo: str, pat: str, path: str):
        """
        Args:
            repo: The target repository (e.g., 'username/estate-vault')
            pat: Fine-grained GitHub PAT with read/write access to the repo
            path: Path within the repository (e.g., 'universal-mail/state.json')
        """
        self.repo = repo
        self.pat = pat
        self.path = path
        self.api_base = f"https://api.github.com/repos/{repo}/contents/{path}"
        self.sha: Optional[str] = None
    
    def pull(self) -> Optional[Dict[str, Any]]:
        """Fetch the state file from the remote vault."""
        req = urllib.request.Request(self.api_base)
        req.add_header("Authorization", f"Bearer {self.pat}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        try:
            with urllib.request.urlopen(req) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    self.sha = data.get("sha")
                    content = base64.b64decode(data.get("content", "")).decode("utf-8")
                    return json.loads(content)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                logger.info(f"Vault state file {self.path} not found. A new one will be created.")
                return None
            logger.error(f"Failed to pull state from vault ({e.code}): {e.reason}")
        except Exception as e:
            logger.error(f"Failed to pull state from vault: {e}")
        return None

    def push(self, data: Dict[str, Any], commit_message: str = "Update state") -> bool:
        """Push the state file to the remote vault, creating a new commit."""
        content_bytes = json.dumps(data, indent=2).encode("utf-8")
        b64_content = base64.b64encode(content_bytes).decode("utf-8")
        
        payload = {
            "message": commit_message,
            "content": b64_content
        }
        if self.sha:
            payload["sha"] = self.sha
            
        req = urllib.request.Request(self.api_base, data=json.dumps(payload).encode("utf-8"), method="PUT")
        req.add_header("Authorization", f"Bearer {self.pat}")
        req.add_header("Accept", "application/vnd.github.v3+json")
        req.add_header("Content-Type", "application/json")
        
        try:
            with urllib.request.urlopen(req) as response:
                if response.status in (200, 201):
                    res_data = json.loads(response.read().decode("utf-8"))
                    self.sha = res_data.get("content", {}).get("sha", self.sha)
                    logger.info(f"Successfully pushed state to vault {self.repo}/{self.path}")
                    return True
        except Exception as e:
            logger.error(f"Failed to push state to vault: {e}")
        return False
