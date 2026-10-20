"""Reuse the existing phone cascade with a virtual maximum width of 430px."""
from pathlib import Path
import sys

import tinycss2


PHONE_WIDTH = 430


def _tokens(tokens, *, media=False):
    result = []
    for token in tokens:
        if media and token.type == "() block":
            significant = [item for item in token.content if item.type != "whitespace"]
            if (len(significant) == 3 and significant[0].type == "ident"
                    and significant[1].type == "literal" and significant[1].value == ":"
                    and significant[2].type == "dimension" and significant[2].lower_unit == "px"):
                name, width = significant[0].lower_value, significant[2].value
                if name == "max-width" and width >= PHONE_WIDTH:
                    token.content = tinycss2.parse_component_value_list("min-width: 0px")
                elif name == "min-width" and width > PHONE_WIDTH:
                    token.content = tinycss2.parse_component_value_list("max-width: 0px")
        if token.type == "dimension" and token.lower_unit in {"vw", "dvw", "svw", "lvw"}:
            operation = "min" if token.value >= 0 else "max"
            cap = token.value * PHONE_WIDTH / 100
            result.extend(tinycss2.parse_component_value_list(
                f"{operation}({token.serialize()}, {cap:g}px)"
            ))
            continue
        if hasattr(token, "arguments"):
            token.arguments = _tokens(token.arguments, media=media)
        if hasattr(token, "content"):
            token.content = _tokens(token.content, media=media)
        result.append(token)
    return result


def phone_styles(source: str) -> str:
    rules = tinycss2.parse_stylesheet(source)
    for rule in rules:
        if rule.type == "error":
            raise ValueError(rule.message)
        if hasattr(rule, "prelude"):
            rule.prelude = _tokens(rule.prelude, media=(
                rule.type == "at-rule" and rule.lower_at_keyword == "media"
            ))
        if getattr(rule, "content", None) is not None:
            rule.content = _tokens(rule.content)
    return tinycss2.serialize(rules)


def build(directory: Path) -> None:
    for name in ("style", "design-system"):
        source = (directory / f"{name}.css").read_text(encoding="utf-8")
        (directory / f"{name}-phone.css").write_text(phone_styles(source), encoding="utf-8")


if __name__ == "__main__":
    build(Path(sys.argv[1]))
