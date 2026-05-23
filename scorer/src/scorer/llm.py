"""Codex CLI subprocess wrapper.

We don't have an OpenAI/Anthropic API key — only OAuth via the codex CLI
(`codex login`). So all LLM calls go through `codex exec --json
--output-schema schema.json -o out.txt` with the prompt fed via stdin.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class LLMError(RuntimeError):
    pass


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def call_codex(
    prompt: str,
    schema_path: Path,
    *,
    timeout: int = 300,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Run codex once with structured output. Returns parsed JSON.

    The prompt is written to stdin. The model's final response is
    constrained by the schema and captured via `-o`. Raises LLMError on
    any failure (non-zero exit, missing output, invalid JSON).
    """
    if not schema_path.exists():
        raise LLMError(f"schema not found: {schema_path}")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as out_file:
        out_path = Path(out_file.name)

    try:
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--output-schema",
            str(schema_path),
            "-o",
            str(out_path),
        ]
        if extra_args:
            cmd.extend(extra_args)
        cmd.append("-")  # read prompt from stdin

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_group(proc)
            stdout, stderr = proc.communicate()
            raise LLMError(
                f"codex timed out after {timeout}s\n"
                f"stderr: {stderr.strip()[-2000:]}"
            ) from exc

        result = subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )

        if result.returncode != 0:
            raise LLMError(
                f"codex exited {result.returncode}\n"
                f"stderr: {result.stderr.strip()[-2000:]}"
            )

        raw = out_path.read_text(encoding="utf-8").strip()
        if not raw:
            raise LLMError(
                "codex produced empty output\n"
                f"stderr: {result.stderr.strip()[-2000:]}"
            )

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"codex output was not valid JSON: {exc}\nraw: {raw[:500]}"
            ) from exc
    finally:
        out_path.unlink(missing_ok=True)
