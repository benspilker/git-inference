from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Git Inference API")
    api_wait_timeout_seconds: int = int(os.getenv("API_WAIT_TIMEOUT_SECONDS", "180"))
    job_timeout_seconds: int = int(os.getenv("JOB_TIMEOUT_SECONDS", "900"))
    worker_poll_interval_seconds: float = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "1.0"))
    result_poll_interval_seconds: float = float(os.getenv("RESULT_POLL_INTERVAL_SECONDS", "2.0"))
    git_max_retries: int = int(os.getenv("GIT_MAX_RETRIES", "3"))
    git_retry_delay_seconds: float = float(os.getenv("GIT_RETRY_DELAY_SECONDS", "1.5"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    db_path: Path = Path(os.getenv("DB_PATH", "/tmp/git_inference_api/jobs.db"))
    repo_path: Path = Path(os.getenv("REPO_PATH", "/tmp/git_inference_api/workrepo"))
    repo_lock_path: Path = Path(os.getenv("REPO_LOCK_PATH", "/tmp/git_inference_api/workrepo.lock"))
    branch: str = os.getenv("REPO_BRANCH", "main")
    requests_dir: str = os.getenv("REQUESTS_DIR", "requests")
    responses_dir: str = os.getenv("RESPONSES_DIR", "responses")
    errors_dir: str = os.getenv("ERRORS_DIR", "errors")
    status_dir: str = os.getenv("STATUS_DIR", "status")
    combined_dir: str = os.getenv("COMBINED_DIR", "combined")
    git_author_name: str = os.getenv("GIT_AUTHOR_NAME", "Git Inference API")
    git_author_email: str = os.getenv("GIT_AUTHOR_EMAIL", "git-inference-api@example.com")
    auto_init_repo: bool = os.getenv("AUTO_INIT_REPO", "false").lower() == "true"
    available_models_csv: str = os.getenv("AVAILABLE_MODELS", "git-chatgpt")
    prompt_chunk_words: int = int(os.getenv("PROMPT_CHUNK_WORDS", "2000"))
    prompt_max_chunks: int = int(os.getenv("PROMPT_MAX_CHUNKS", "5"))

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self.repo_lock_path.parent.mkdir(parents=True, exist_ok=True)

    def available_models(self) -> list[str]:
        names = [name.strip() for name in self.available_models_csv.split(",")]
        deduped: list[str] = []
        for name in names:
            if name and name not in deduped:
                deduped.append(name)
        return deduped


settings = Settings()



