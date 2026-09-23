"""Claude API 연결 (장면 판단, 영상 설명, 후보 선택에 사용)."""

from __future__ import annotations

import base64
import json
from typing import Any

DEFAULT_MODEL = "claude-opus-5"


class AIError(RuntimeError):
    pass


def image_block(jpeg: bytes, media_type: str = "image/jpeg") -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type,
                   "data": base64.standard_b64encode(jpeg).decode("ascii")},
    }


def text_block(text: str, cache: bool = False) -> dict:
    block: dict[str, Any] = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


class Claude:
    """JSON 형식으로 답을 받는 간단한 래퍼.

    API 키는 인자 > ANTHROPIC_API_KEY 환경변수 > `ant auth login` 프로필 순서로 찾는다.
    """

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        try:
            import anthropic
        except ImportError as exc:
            raise AIError("AI 기능을 쓰려면 'pip install anthropic' 을 실행하세요.") from exc
        self._anthropic = anthropic
        try:
            self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        except anthropic.AnthropicError as exc:
            raise AIError("Claude API 키가 없습니다. AI 설정에서 키를 입력하세요.") from exc
        self.model = model or DEFAULT_MODEL

    def ask_json(self, system: list[dict], content: list[dict], schema: dict,
                 max_tokens: int = 16000) -> dict:
        anthropic = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                # 안전 분류기가 요청을 거절하면 서버가 권장 모델로 다시 시도한다
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                thinking={"type": "adaptive"},
                output_config={"format": {"type": "json_schema", "schema": schema}},
                system=system,
                messages=[{"role": "user", "content": content}],
            )
        except TypeError as exc:
            if "authentication" in str(exc).lower():
                raise AIError("Claude API 키가 없습니다. AI 설정에서 키를 입력하세요.") from exc
            raise
        except anthropic.AuthenticationError as exc:
            raise AIError("Claude API 키가 올바르지 않습니다. 설정에서 키를 확인하세요.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AIError(f"Claude API 권한이 없습니다: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise AIError("Claude API 사용량 한도에 걸렸습니다. 잠시 뒤 다시 시도하세요.") from exc
        except anthropic.APIConnectionError as exc:
            raise AIError("Claude API 에 연결할 수 없습니다. 인터넷 연결을 확인하세요.") from exc
        except anthropic.APIStatusError as exc:
            raise AIError(f"Claude API 오류 ({exc.status_code}): {exc.message}") from exc

        if response.stop_reason == "refusal":
            raise AIError("Claude 가 이 요청을 처리하지 않았습니다 (안전 정책).")
        if response.stop_reason == "max_tokens":
            raise AIError("Claude 응답이 너무 길어 잘렸습니다. 장면 수를 줄여 다시 시도하세요.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise AIError("Claude 응답을 해석할 수 없습니다.") from exc
