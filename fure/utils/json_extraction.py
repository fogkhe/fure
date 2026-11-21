import json
import math
import re

from fure.core.types import JSONValue


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


DECODER = json.JSONDecoder(
    object_pairs_hook=_object, parse_float=_float, parse_constant=_constant
)


def parse_json(text: str) -> JSONValue:
    return DECODER.decode(text)


def strip_reasoning(text: str, reasoning_end_tags: tuple[str, ...]) -> str:
    # Only explicitly configured markers are stripped. An empty list disables it.
    end = max(
        (
            match.end()
            for tag in reasoning_end_tags
            for match in re.finditer(re.escape(tag), text, re.IGNORECASE)
        ),
        default=0,
    )
    return text[end:].strip()


def extract_json(text: str, reasoning_end_tags: tuple[str, ...] = ()) -> JSONValue:
    try:
        return parse_json(text.strip())
    except ValueError:
        pass
    text = strip_reasoning(text, reasoning_end_tags)
    try:
        return parse_json(text)
    except ValueError, json.JSONDecodeError:
        pass

    blocks = list(
        re.finditer(
            r'```(?:json)?\s*((?:"(?:\\.|[^"\\])*"|[^"])*?)\s*```',
            text,
            re.IGNORECASE | re.DOTALL,
        )
    )
    if blocks:
        if len(blocks) != 1:
            raise ValueError("Chat completion must contain exactly one JSON value")
        # Prose around a fenced result is permitted, additional JSON is not.
        outside = text[: blocks[0].start()] + text[blocks[0].end() :]
        if _candidates(outside):
            raise ValueError("Chat completion must contain exactly one JSON value")
        return parse_json(blocks[0].group(1).strip())

    values = _candidates(text)
    if len(values) != 1:
        raise ValueError("Chat completion must contain exactly one JSON value")
    return values[0]


def _candidates(text: str) -> list[JSONValue]:
    values = []
    index = 0
    while index < len(text):
        char = text[index]
        if char not in '[{"-0123456789tfn':
            index += 1
            continue
        if (
            char not in '[{"'
            and index > 0
            and (text[index - 1].isalnum() or text[index - 1] == "_")
        ):
            index += 1
            continue
        try:
            value, end = DECODER.raw_decode(text, index)
        except ValueError as error:
            # Do not salvage a valid nested value from a broken outer container.
            if char in '[{"':
                raise ValueError("Chat completion returned invalid JSON") from error
            index += 1
            continue
        if end < len(text) and (text[end].isalnum() or text[end] == "_"):
            # Literal-like prefixes in ordinary words are not JSON tokens.
            index = end
            continue
        values.append(value)
        index = end
    return values
