#!/usr/bin/env python3
"""Fill missing website translations with a local Ollama model.

The script treats ``locales/en.json`` as the source of truth and updates only
missing, null, or whitespace-only values in the other locale JSON files. It is
safe to stop and rerun: every completed batch is written atomically, and
existing non-empty translations are left untouched.

Examples:
    python3 translate_locales.py --dry-run
    python3 translate_locales.py --languages ko,ja,fr-CA
    python3 translate_locales.py --model gpt-oss:20b
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Iterable, Sequence
from urllib import error, request


DEFAULT_HOST = "http://localhost:11434"
DEFAULT_SOURCE_LANGUAGE = "en"

LANGUAGE_NAMES = {
    "af": "Afrikaans",
    "am": "Amharic",
    "ar": "Arabic",
    "az": "Azerbaijani",
    "be": "Belarusian",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "bs": "Bosnian",
    "ca": "Catalan",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "es": "Spanish",
    "es-MX": "Mexican Spanish",
    "es-US": "Spanish (United States)",
    "et": "Estonian",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "fr-CA": "Canadian French",
    "gu": "Gujarati",
    "ha": "Hausa",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "hy": "Armenian",
    "id": "Indonesian",
    "ig": "Igbo",
    "it": "Italian",
    "ja": "Japanese",
    "ka": "Georgian",
    "kk": "Kazakh",
    "kn": "Kannada",
    "ko": "Korean",
    "ln": "Lingala",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mk": "Macedonian",
    "ml": "Malayalam",
    "mn": "Mongolian",
    "mr": "Marathi",
    "ms": "Malay",
    "my": "Burmese",
    "nb": "Norwegian Bokmål",
    "ne": "Nepali",
    "nl": "Dutch",
    "om": "Oromo",
    "or": "Odia",
    "pa": "Punjabi (Gurmukhi)",
    "pcm": "Nigerian Pidgin",
    "pl": "Polish",
    "pt-BR": "Brazilian Portuguese",
    "pt-PT": "European Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "rw": "Kinyarwanda",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "so": "Somali",
    "sq": "Albanian",
    "sr": "Serbian",
    "st": "Southern Sotho",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "ti": "Tigrinya",
    "tr": "Turkish",
    "ts": "Tsonga (XiTsonga)",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "vi": "Vietnamese",
    "yo": "Yoruba",
    "zh-Hans": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese",
    "zh-HK": "Traditional Chinese (Hong Kong)",
    "zu": "Zulu",
}

HTML_TAG_RE = re.compile(r"</?[^>]+?>")
NUMBER_RE = re.compile(r"(?<![\w])\d+(?:[.,]\d+)*(?:\+)?")
PLACEHOLDER_RE = re.compile(
    r"https?://[^\s<>\"]+"
    r"|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"
    r"|\{\{[^{}]+\}\}"
    r"|\$\{[^{}]+\}"
    r"|%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[a-zA-Z@]"
)
PROTECTED_NAMES = (
    "GGanbu Chat",
    "GGanbu",
    "Ollama",
    "Bluetooth",
    "RSSI",
    "iOS",
    "Android",
    "Apple App Store",
    "Google Play",
)
MISSING = object()


class TranslationError(RuntimeError):
    """Raised when Ollama cannot produce a valid translation batch."""


@dataclass(frozen=True)
class TranslationItem:
    path: tuple[str, ...]
    source: str

    @property
    def display_path(self) -> str:
        return " / ".join(self.path)


class OllamaClient:
    def __init__(
        self,
        host: str,
        timeout: float,
        temperature: float,
        keep_alive: str,
    ) -> None:
        normalized = host.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            normalized = f"http://{normalized}"
        if normalized.endswith("/api"):
            normalized = normalized[:-4]
        self.host = normalized
        self.timeout = timeout
        self.temperature = temperature
        self.keep_alive = keep_alive

    def _request(self, method: str, path: str, payload: Any = None) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        http_request = request.Request(
            f"{self.host}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with request.urlopen(http_request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TranslationError(
                f"Ollama returned HTTP {exc.code} for {path}: {detail}"
            ) from exc
        except error.URLError as exc:
            raise TranslationError(
                f"Cannot reach Ollama at {self.host}: {exc.reason}"
            ) from exc

        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslationError(
                f"Ollama returned invalid JSON for {path}: {raw[:300]}"
            ) from exc
        if isinstance(value, dict) and value.get("error"):
            raise TranslationError(f"Ollama error: {value['error']}")
        return value

    def list_models(self) -> list[str]:
        result = self._request("GET", "/api/tags")
        models = result.get("models", []) if isinstance(result, dict) else []
        names: list[str] = []
        for model in models:
            if not isinstance(model, dict):
                continue
            name = model.get("name") or model.get("model")
            if isinstance(name, str) and name:
                names.append(name)
        return names

    def generate(self, model: str, system: str, prompt: str, ids: Sequence[str]) -> str:
        schema = {
            "type": "object",
            "properties": {item_id: {"type": "string"} for item_id in ids},
            "required": list(ids),
            "additionalProperties": False,
        }
        result = self._request(
            "POST",
            "/api/generate",
            {
                "model": model,
                "system": system,
                "prompt": prompt,
                "stream": False,
                "format": schema,
                "keep_alive": self.keep_alive,
                "options": {"temperature": self.temperature},
            },
        )
        response_text = result.get("response") if isinstance(result, dict) else None
        if not isinstance(response_text, str) or not response_text.strip():
            raise TranslationError("Ollama returned an empty generation response.")
        return response_text


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            value = json.load(file)
    except FileNotFoundError as exc:
        raise TranslationError(f"Locale file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TranslationError(f"The root value in {path} must be a JSON object.")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary_path, existing_mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def flatten_source_strings(
    value: Any, path: tuple[str, ...] = ()
) -> Iterable[TranslationItem]:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TranslationError(f"Non-string JSON key at {' / '.join(path)}")
            yield from flatten_source_strings(child, (*path, key))
        return
    if isinstance(value, str):
        yield TranslationItem(path=path, source=value)
        return
    raise TranslationError(
        f"Source value at {' / '.join(path)} must be a string, not {type(value).__name__}."
    )


def get_nested(value: dict[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return MISSING
        current = current[key]
    return current


def set_nested(value: dict[str, Any], path: Sequence[str], translated: str) -> None:
    current = value
    for key in path[:-1]:
        child = current.get(key, MISSING)
        if child is MISSING:
            child = {}
            current[key] = child
        elif not isinstance(child, dict):
            raise TranslationError(
                f"Cannot write {' / '.join(path)} because {' / '.join(path[:-1])} "
                "is not a JSON object."
            )
        current = child
    current[path[-1]] = translated


def is_missing_or_blank(value: Any) -> bool:
    return value is MISSING or value is None or (
        isinstance(value, str) and not value.strip()
    )


def missing_items(
    source_items: Sequence[TranslationItem], destination: dict[str, Any]
) -> list[TranslationItem]:
    return [
        item
        for item in source_items
        if is_missing_or_blank(get_nested(destination, item.path))
    ]


def chunks(items: Sequence[TranslationItem], size: int) -> Iterable[list[TranslationItem]]:
    for offset in range(0, len(items), size):
        yield list(items[offset : offset + size])


def parse_generated_object(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
        candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            raise TranslationError("The model response does not contain a JSON object.")
        try:
            value = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise TranslationError(f"Invalid translation JSON: {exc}") from exc
    if isinstance(value, dict) and isinstance(value.get("translations"), dict):
        value = value["translations"]
    if not isinstance(value, dict):
        raise TranslationError("The model response must be a JSON object.")
    return value


def validation_problem(source: str, translated: Any) -> str | None:
    if not isinstance(translated, str) or not translated.strip():
        return "empty or non-string translation"

    if HTML_TAG_RE.findall(source) != HTML_TAG_RE.findall(translated):
        return "HTML tags were changed"

    protected_source = Counter(PLACEHOLDER_RE.findall(source))
    protected_translation = Counter(PLACEHOLDER_RE.findall(translated))
    if protected_source != protected_translation:
        return "URL, email, or placeholder tokens were changed"

    if Counter(NUMBER_RE.findall(source)) != Counter(NUMBER_RE.findall(translated)):
        return "numeric values were changed"

    for name in PROTECTED_NAMES:
        if name in source and name not in translated:
            return f"protected name {name!r} was changed"
    return None


def build_prompts(
    language_code: str,
    language_name: str,
    items_by_id: dict[str, TranslationItem],
    retry_notes: dict[str, str] | None = None,
) -> tuple[str, str]:
    system = (
        "You are a professional software localization and privacy-policy translator. "
        f"Translate English into {language_name} ({language_code}). Use natural, clear "
        "language suitable for an adult consumer app. Preserve the complete legal and "
        "technical meaning without adding, omitting, softening, or inventing claims. "
        "Return only the JSON object requested by the user."
    )

    rules = [
        "Return one non-empty translated string for every input ID and no extra keys.",
        "Keep JSON IDs unchanged; translate values only.",
        "Preserve every HTML tag exactly, including attributes, quotes, and order.",
        "Preserve URLs, email addresses, placeholders, and numeric values exactly.",
        "Keep these product and technology names unchanged when present: GGanbu Chat, "
        "GGanbu, Ollama, Bluetooth, RSSI, iOS, Android, Apple App Store, Google Play.",
        "Do not wrap the JSON in Markdown or add explanations.",
    ]
    if retry_notes:
        details = "; ".join(
            f"{item_id}: previous output had {problem}"
            for item_id, problem in retry_notes.items()
        )
        rules.append(f"Correct these previous validation failures: {details}.")

    payload = {item_id: item.source for item_id, item in items_by_id.items()}
    prompt = (
        "Translate the following JSON object.\n\nRequirements:\n- "
        + "\n- ".join(rules)
        + "\n\nInput JSON:\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    return system, prompt


def translate_batch(
    client: OllamaClient,
    model: str,
    language_code: str,
    language_name: str,
    items: Sequence[TranslationItem],
    retries: int,
) -> dict[tuple[str, ...], str]:
    pending = {f"item_{index:03d}": item for index, item in enumerate(items, start=1)}
    completed: dict[tuple[str, ...], str] = {}
    retry_notes: dict[str, str] = {}
    last_error = "unknown validation error"

    for attempt in range(1, retries + 1):
        system, prompt = build_prompts(
            language_code, language_name, pending, retry_notes or None
        )
        try:
            response = client.generate(model, system, prompt, list(pending))
            generated = parse_generated_object(response)
        except (TranslationError, TimeoutError) as exc:
            last_error = str(exc)
            if attempt < retries:
                delay = min(2 ** (attempt - 1), 8)
                print(f"    attempt {attempt}/{retries} failed: {exc}; retrying in {delay}s")
                time.sleep(delay)
                continue
            break

        next_pending: dict[str, TranslationItem] = {}
        retry_notes = {}
        for item_id, item in pending.items():
            translated = generated.get(item_id, MISSING)
            problem = validation_problem(item.source, translated)
            if problem:
                next_pending[item_id] = item
                retry_notes[item_id] = problem
                last_error = f"{item.display_path}: {problem}"
            else:
                completed[item.path] = translated.strip()

        pending = next_pending
        if not pending:
            return completed
        if attempt < retries:
            paths = ", ".join(item.display_path for item in pending.values())
            print(f"    retrying {len(pending)} invalid item(s): {paths}")

    unresolved = ", ".join(item.display_path for item in pending.values())
    raise TranslationError(
        f"Unable to translate {unresolved or 'batch'} after {retries} attempts: {last_error}"
    )


def select_model(client: OllamaClient, requested_model: str | None) -> str:
    models = client.list_models()
    if not models:
        raise TranslationError(
            f"No Ollama models are installed at {client.host}. Pull a translation-capable model first."
        )
    if requested_model:
        if requested_model not in models:
            raise TranslationError(
                f"Model {requested_model!r} is not installed. Available models: {', '.join(models)}"
            )
        return requested_model

    preferred_prefixes = (
        "translategemma",
        "gpt-oss",
        "gemma4",
        "qwen3",
        "gemma3",
        "llama3",
    )
    usable = [model for model in models if "embed" not in model.lower()]
    for prefix in preferred_prefixes:
        for model in usable:
            if model.lower().startswith(prefix):
                return model
    return (usable or models)[0]


def parse_requested_languages(raw: str | None, available: Sequence[str]) -> list[str]:
    if not raw:
        return sorted(code for code in available if code != DEFAULT_SOURCE_LANGUAGE)
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [code for code in requested if code not in available]
    if unknown:
        raise TranslationError(
            f"Unknown or missing locale file(s): {', '.join(unknown)}"
        )
    if DEFAULT_SOURCE_LANGUAGE in requested:
        raise TranslationError("The English source locale cannot be a translation target.")
    return list(dict.fromkeys(requested))


def parse_pages(raw: str | None, source: dict[str, Any]) -> set[str] | None:
    if not raw:
        return None
    pages = {part.strip() for part in raw.split(",") if part.strip()}
    unknown = pages.difference(source)
    if unknown:
        raise TranslationError(f"Unknown source section(s): {', '.join(sorted(unknown))}")
    return pages


def build_argument_parser() -> argparse.ArgumentParser:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description=(
            "Translate missing or blank locale values from locales/en.json using "
            "an Ollama server. Existing non-empty translations are preserved."
        )
    )
    parser.add_argument(
        "--locales-dir",
        type=Path,
        default=script_dir / "locales",
        help="locale JSON directory (default: %(default)s)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("OLLAMA_HOST", DEFAULT_HOST),
        help="Ollama base URL (default: OLLAMA_HOST or %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("OLLAMA_MODEL"),
        help="installed Ollama model; auto-selects a suitable model when omitted",
    )
    parser.add_argument(
        "--languages",
        help="comma-separated locale codes to process; default: every non-English JSON file",
    )
    parser.add_argument(
        "--pages",
        help="comma-separated top-level sections such as index,privacy; default: all",
    )
    parser.add_argument(
        "--batch-size", type=int, default=8, help="strings per Ollama request (default: %(default)s)"
    )
    parser.add_argument(
        "--retries", type=int, default=4, help="attempts per invalid batch (default: %(default)s)"
    )
    parser.add_argument(
        "--timeout", type=float, default=300.0, help="HTTP timeout in seconds (default: %(default)s)"
    )
    parser.add_argument(
        "--temperature", type=float, default=0.1, help="model temperature (default: %(default)s)"
    )
    parser.add_argument(
        "--keep-alive", default="10m", help="Ollama model keep-alive duration (default: %(default)s)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show missing counts without contacting Ollama or changing files",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="list installed Ollama models and exit",
    )
    return parser


def run(args: argparse.Namespace) -> int:
    if args.batch_size < 1:
        raise TranslationError("--batch-size must be at least 1.")
    if args.retries < 1:
        raise TranslationError("--retries must be at least 1.")

    locales_dir = args.locales_dir.resolve()
    source_path = locales_dir / f"{DEFAULT_SOURCE_LANGUAGE}.json"
    source = load_json_object(source_path)
    selected_pages = parse_pages(args.pages, source)
    source_items = [
        item
        for item in flatten_source_strings(source)
        if selected_pages is None or (item.path and item.path[0] in selected_pages)
    ]

    locale_paths = {
        path.stem: path
        for path in locales_dir.glob("*.json")
        if path.is_file()
    }
    languages = parse_requested_languages(args.languages, list(locale_paths))

    work: list[tuple[str, Path, dict[str, Any], list[TranslationItem]]] = []
    total_missing = 0
    for language in languages:
        destination_path = locale_paths[language]
        destination = load_json_object(destination_path)
        items = missing_items(source_items, destination)
        work.append((language, destination_path, destination, items))
        total_missing += len(items)

    print(
        f"Source: {source_path}\n"
        f"Targets: {len(languages)} language(s)\n"
        f"Missing or blank values: {total_missing}"
    )
    for language, _, _, items in work:
        print(f"  {language:8} {len(items):3} missing")

    if args.dry_run or total_missing == 0:
        return 0

    client = OllamaClient(
        host=args.host,
        timeout=args.timeout,
        temperature=args.temperature,
        keep_alive=args.keep_alive,
    )
    if args.list_models:
        for model in client.list_models():
            print(model)
        return 0

    model = select_model(client, args.model)
    print(f"Using Ollama model: {model} at {client.host}")

    failures: list[tuple[str, str]] = []
    translated_total = 0
    for language, destination_path, destination, items in work:
        if not items:
            continue
        language_name = LANGUAGE_NAMES.get(language, language)
        batch_count = (len(items) + args.batch_size - 1) // args.batch_size
        print(f"\n[{language}] {language_name}: {len(items)} values in {batch_count} batch(es)")

        try:
            for batch_number, batch in enumerate(chunks(items, args.batch_size), start=1):
                print(f"  batch {batch_number}/{batch_count}: translating {len(batch)} value(s)")
                translations = translate_batch(
                    client=client,
                    model=model,
                    language_code=language,
                    language_name=language_name,
                    items=batch,
                    retries=args.retries,
                )
                for item in batch:
                    set_nested(destination, item.path, translations[item.path])
                atomic_write_json(destination_path, destination)
                translated_total += len(batch)
        except TranslationError as exc:
            failures.append((language, str(exc)))
            print(f"  ERROR: {exc}", file=sys.stderr)
            continue

    print(f"\nTranslated and saved: {translated_total} value(s)")
    if failures:
        print(f"Failed languages: {len(failures)}", file=sys.stderr)
        for language, message in failures:
            print(f"  {language}: {message}", file=sys.stderr)
        print("Run the same command again to resume the remaining values.", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    try:
        if args.list_models:
            client = OllamaClient(
                host=args.host,
                timeout=args.timeout,
                temperature=args.temperature,
                keep_alive=args.keep_alive,
            )
            for model in client.list_models():
                print(model)
            return 0
        return run(args)
    except TranslationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Completed batches are already saved; rerun to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
