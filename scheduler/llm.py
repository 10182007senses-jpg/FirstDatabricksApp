"""LLM 呼び出し(Databricks Foundation Model API)。

個人情報の扱い(要件定義書 10章):講師の名前は LLM に渡しません。
送る前に「講師A」「講師B」…に置き換え、返ってきた文の記号を名前に戻します。
エンドポイントが未設定・呼び出し失敗のときは None を返し、呼び出し側が定型文に切り替えます。
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _alias(i: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return "講師" + (letters[i] if i < 26 else letters[i // 26 - 1] + letters[i % 26])


class NameMasker:
    """講師名 ⇔ 記号 の置き換え。"""

    def __init__(self, names: list[str]):
        # 長い名前から置き換える(「田中」と「田中太郎」のような包含に備える)
        unique = sorted({n for n in names if n}, key=lambda n: (-len(n), n))
        self.to_alias = {name: _alias(i) for i, name in enumerate(unique)}

    def mask(self, text: str) -> str:
        for name, alias in self.to_alias.items():
            text = text.replace(name, alias)
        return text

    def unmask(self, text: str) -> str:
        # 「講師AB」を「講師A」より先に戻す
        for name, alias in sorted(self.to_alias.items(), key=lambda kv: -len(kv[1])):
            text = text.replace(alias, name)
        return text


class LLMClient:
    def __init__(self, endpoint: str | None = None, transport=None):
        """transport はテスト用。(system, user) を受け取り文字列を返す関数。"""
        self.endpoint = endpoint if endpoint is not None else os.getenv("SERVING_ENDPOINT", "")
        self._transport = transport

    @property
    def available(self) -> bool:
        return bool(self._transport or self.endpoint)

    def complete(self, system: str, user: str, teacher_names: list[str], max_tokens: int = 4000) -> str | None:
        if not self.available:
            return None
        masker = NameMasker(teacher_names)
        system_m, user_m = masker.mask(system), masker.mask(user)
        try:
            if self._transport:
                answer = self._transport(system_m, user_m)
            else:
                answer = self._call_endpoint(system_m, user_m, max_tokens)
        except Exception as exc:  # LLM が落ちていても割り当て作業は続けられるようにする
            log.warning("LLM の呼び出しに失敗しました: %s", exc)
            return None
        return masker.unmask(answer) if answer else None

    def _call_endpoint(self, system: str, user: str, max_tokens: int) -> str:
        import requests
        from databricks.sdk.core import Config

        cfg = Config()
        host = cfg.host if cfg.host.startswith("http") else f"https://{cfg.host}"
        headers = dict(cfg.authenticate())
        headers["Content-Type"] = "application/json"
        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if "." in self.endpoint:
            # Unity Catalog のモデルサービス(例: system.ai.gemma-3-12b)は AI Gateway で呼ぶ
            url = f"{host}/ai-gateway/mlflow/v1/chat/completions"
            payload["model"] = self.endpoint
        else:
            # 従来のモデルサービングエンドポイント(例: databricks-gpt-oss-20b)
            url = f"{host}/serving-endpoints/{self.endpoint}/invocations"
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):  # 一部のモデルは content をブロックの配列で返す
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content
