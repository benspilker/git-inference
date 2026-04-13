from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_csv(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "Git Inference API")

    # API / worker timing
    api_wait_timeout_seconds: int = int(os.getenv("API_WAIT_TIMEOUT_SECONDS", "300"))
    job_timeout_seconds: int = int(os.getenv("JOB_TIMEOUT_SECONDS", "900"))
    worker_poll_interval_seconds: float = float(os.getenv("WORKER_POLL_INTERVAL_SECONDS", "1.0"))
    result_poll_interval_seconds: float = float(os.getenv("RESULT_POLL_INTERVAL_SECONDS", "2.0"))

    # Optional stage-specific timeouts
    router_stage_timeout_seconds: int = int(os.getenv("ROUTER_STAGE_TIMEOUT_SECONDS", "900"))
    planner_stage_timeout_seconds: int = int(os.getenv("PLANNER_STAGE_TIMEOUT_SECONDS", "120"))
    answerer_stage_timeout_seconds: int = int(os.getenv("ANSWERER_STAGE_TIMEOUT_SECONDS", "180"))
    final_phraser_stage_timeout_seconds: int = int(os.getenv("FINAL_PHRASER_STAGE_TIMEOUT_SECONDS", "120"))
    execution_stage_timeout_seconds: int = int(os.getenv("EXECUTION_STAGE_TIMEOUT_SECONDS", "300"))
    verification_stage_timeout_seconds: int = int(os.getenv("VERIFICATION_STAGE_TIMEOUT_SECONDS", "120"))
    clarification_timeout_seconds: int = int(os.getenv("CLARIFICATION_TIMEOUT_SECONDS", "86400"))

    # Git behavior
    git_max_retries: int = int(os.getenv("GIT_MAX_RETRIES", "3"))
    git_retry_delay_seconds: float = float(os.getenv("GIT_RETRY_DELAY_SECONDS", "1.5"))
    git_author_name: str = os.getenv("GIT_AUTHOR_NAME", "Git Inference API")
    git_author_email: str = os.getenv("GIT_AUTHOR_EMAIL", "git-inference-api@example.com")
    auto_init_repo: bool = env_bool("AUTO_INIT_REPO", False)
    branch: str = os.getenv("REPO_BRANCH", "main")

    # Logging
    log_level: str = os.getenv("LOG_LEVEL", "INFO")

    # Paths
    db_path: Path = Path(os.getenv("DB_PATH", "/tmp/git_inference_api/jobs.db"))
    repo_path: Path = Path(os.getenv("REPO_PATH", "/tmp/git_inference_api/workrepo"))
    repo_lock_path: Path = Path(os.getenv("REPO_LOCK_PATH", "/tmp/git_inference_api/workrepo.lock"))

    # Artifact directories
    requests_dir: str = os.getenv("REQUESTS_DIR", "requests")
    responses_dir: str = os.getenv("RESPONSES_DIR", "responses")
    readable_dir: str = os.getenv("READABLE_DIR", "responses_readable")
    errors_dir: str = os.getenv("ERRORS_DIR", "errors")
    status_dir: str = os.getenv("STATUS_DIR", "status")
    combined_dir: str = os.getenv("COMBINED_DIR", "combined")
    evaluations_dir: str = os.getenv("EVALUATIONS_DIR", "evaluations")
    stages_dir: str = os.getenv("STAGES_DIR", "stages")
    execution_dir: str = os.getenv("EXECUTION_DIR", "execution")
    old_requests_dir: str = os.getenv("OLD_REQUESTS_DIR", "requests/old-requests")
    old_responses_dir: str = os.getenv("OLD_RESPONSES_DIR", "responses/old-responses")

    # Model config
    available_models_csv: str = env_csv("AVAILABLE_MODELS", "git-chatgpt-json,git-chatgpt,git-perplexity,git-grok")
    openclaw_compat_models_csv: str = env_csv("OPENCLAW_COMPAT_MODELS", "git-chatgpt,git-perplexity,git-grok")

    # Prompt / chunking
    prompt_chunk_words: int = int(os.getenv("PROMPT_CHUNK_WORDS", "2000"))
    prompt_max_chunks: int = int(os.getenv("PROMPT_MAX_CHUNKS", "5"))

    # Queue/admin behavior
    stale_inflight_max_age_seconds: int = int(os.getenv("STALE_INFLIGHT_MAX_AGE_SECONDS", "900"))
    admin_api_key: str = os.getenv("ADMIN_API_KEY", "")
    heartbeat_short_circuit_enabled: bool = env_bool("HEARTBEAT_SHORT_CIRCUIT_ENABLED", True)
    heartbeat_defer_when_busy: bool = env_bool("HEARTBEAT_DEFER_WHEN_BUSY", True)
    heartbeat_cooldown_seconds: int = int(os.getenv("HEARTBEAT_COOLDOWN_SECONDS", "1800"))
    heartbeat_ack_text: str = os.getenv("HEARTBEAT_ACK_TEXT", "HEARTBEAT_OK")

    # Git lock cleanup
    enable_stale_git_lock_cleanup: bool = env_bool("ENABLE_STALE_GIT_LOCK_CLEANUP", True)
    git_lock_stale_seconds: int = int(os.getenv("GIT_LOCK_STALE_SECONDS", "120"))

    # Prompt file locations
    router_prompt_file: str = os.getenv("ROUTER_PROMPT_FILE", ".github/workflows/pipeline.router_prompt.txt")
    planner_prompt_file: str = os.getenv("PLANNER_PROMPT_FILE", ".github/workflows/pipeline.planner_prompt.txt")
    answerer_prompt_file: str = os.getenv("ANSWERER_PROMPT_FILE", ".github/workflows/pipeline.answerer_prompt.txt")
    final_phraser_prompt_file: str = os.getenv("FINAL_PHRASER_PROMPT_FILE", ".github/workflows/pipeline.final_phraser_prompt.txt")
    eval_prompt_template_file: str = os.getenv("EVAL_PROMPT_TEMPLATE_FILE", ".github/workflows/pipeline.eval_prompt.template.txt")

    # Workflow feature flags
    enable_workflow: bool = env_bool("ENABLE_WORKFLOW", True)
    enable_stage_orchestration: bool = env_bool("ENABLE_STAGE_ORCHESTRATION", False)
    enable_stage_artifacts: bool = env_bool("ENABLE_STAGE_ARTIFACTS", True)
    enable_execution_artifacts: bool = env_bool("ENABLE_EXECUTION_ARTIFACTS", True)
    enable_local_execution: bool = env_bool("ENABLE_LOCAL_EXECUTION", True)
    # Optional bridge: consume scheduled_weather_report handoff and create OpenClaw cron remotely.
    enable_runtime_handoff_executor: bool = env_bool("ENABLE_RUNTIME_HANDOFF_EXECUTOR", False)
    enable_clarification_state: bool = env_bool("ENABLE_CLARIFICATION_STATE", True)
    default_combined_in_message: bool = env_bool("DEFAULT_COMBINED_IN_MESSAGE", False)

    # OpenClaw cron bridge settings (used only when ENABLE_RUNTIME_HANDOFF_EXECUTOR=true)
    openclaw_cron_ssh_target: str = os.getenv("OPENCLAW_CRON_SSH_TARGET", "")
    openclaw_cron_cli_path: str = os.getenv("OPENCLAW_CRON_CLI_PATH", "~/.npm-global/bin/openclaw")
    openclaw_cron_agent: str = os.getenv("OPENCLAW_CRON_AGENT", "main")
    openclaw_cron_channel: str = os.getenv("OPENCLAW_CRON_CHANNEL", "telegram")
    openclaw_cron_to: str = os.getenv("OPENCLAW_CRON_TO", "")
    openclaw_cron_timeout_seconds: int = int(os.getenv("OPENCLAW_CRON_TIMEOUT_SECONDS", "120"))
    openclaw_cron_windows_ssh_path: str = os.getenv(
        "OPENCLAW_CRON_WINDOWS_SSH_PATH",
        "/mnt/c/Windows/System32/OpenSSH/ssh.exe",
    )

    # Optional defaults for execution/planning
    default_user_timezone: str = os.getenv("DEFAULT_USER_TIMEZONE", "America/New_York")
    default_delivery_target: str = os.getenv("DEFAULT_DELIVERY_TARGET", "current_telegram_chat")
    default_question_task_type: str = os.getenv("DEFAULT_QUESTION_TASK_TYPE", "general_question")
    default_research_task_type: str = os.getenv("DEFAULT_RESEARCH_TASK_TYPE", "root_topic_research")
    default_weather_task_type: str = os.getenv("DEFAULT_WEATHER_TASK_TYPE", "scheduled_weather_report")

    def ensure_directories(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.repo_path.mkdir(parents=True, exist_ok=True)
        self.repo_lock_path.parent.mkdir(parents=True, exist_ok=True)

        for rel in (
            self.requests_dir,
            self.responses_dir,
            self.readable_dir,
            self.errors_dir,
            self.status_dir,
            self.combined_dir,
            self.evaluations_dir,
            self.stages_dir,
            self.execution_dir,
            self.old_requests_dir,
            self.old_responses_dir,
        ):
            (self.repo_path / rel).mkdir(parents=True, exist_ok=True)

    def available_models(self) -> list[str]:
        names = [name.strip() for name in self.available_models_csv.split(",")]
        deduped: list[str] = []
        for name in names:
            if name and name not in deduped:
                deduped.append(name)
        return deduped

    def openclaw_compat_models(self) -> list[str]:
        names = [name.strip() for name in self.openclaw_compat_models_csv.split(",")]
        deduped: list[str] = []
        for name in names:
            if name and name not in deduped:
                deduped.append(name)
        return deduped

    @property
    def visible_nonterminal_states(self) -> tuple[str, ...]:
        return ("needs_clarification",)

    @property
    def active_status_values(self) -> tuple[str, ...]:
        return ("routing", "planning", "executing", "verifying")

    @property
    def requeueable_status_values(self) -> tuple[str, ...]:
        return ("routing", "planning", "executing", "verifying")


settings = Settings()
