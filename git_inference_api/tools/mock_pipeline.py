from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, text=True)


def main(repo_path: str, branch: str = "main") -> int:
    repo = Path(repo_path)
    requests_dir = repo / "requests"
    responses_dir = repo / "responses"
    responses_dir.mkdir(parents=True, exist_ok=True)

    while True:
        run("fetch", "origin", branch, cwd=repo)
        run("reset", "--hard", f"origin/{branch}", cwd=repo)
        requests = sorted(requests_dir.glob("job_*.json"))
        for req_path in requests:
            payload = json.loads(req_path.read_text(encoding="utf-8"))
            job_id = payload["job_id"]
            resp_path = responses_dir / f"{job_id}.json"
            if resp_path.exists():
                continue
            chunk_list = payload.get("user_prompt_chunks")
            if not isinstance(chunk_list, list) or not chunk_list:
                chunk_list = payload.get("prompt_chunks")
            if not isinstance(chunk_list, list) or not chunk_list:
                chunk_list = [payload.get("user_prompt", "")]
            prompt_text = "\n\n".join(str(x) for x in chunk_list if x is not None)
            chunk_count = len([x for x in chunk_list if x is not None])
            response = {
                "job_id": job_id,
                "message": {
                    "role": "assistant",
                    "content": f"mock response ({chunk_count} chunk(s)): {prompt_text}",
                },
                "done": True,
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            resp_path.write_text(json.dumps(response, indent=2) + "\n", encoding="utf-8")
            run("add", str(resp_path.relative_to(repo)), cwd=repo)
            run("commit", "-m", f"mock response for {job_id}", cwd=repo)
            run("push", "origin", branch, cwd=repo)
        time.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python tools/mock_pipeline.py /path/to/repo [branch]")
    raise SystemExit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "main"))
