"""Tool discovery and relevance ranking. Discovery never executes tools."""
from dataclasses import dataclass
import re
from .registry import TOOLS, Tool


@dataclass(frozen=True)
class DiscoveryResult:
    tool: Tool
    score: int
    matched_terms: tuple[str, ...]


def _terms(value: str) -> set[str]:
    """Normalize natural-language terms, separators and simple plurals."""
    normalized = re.sub(r"[\W_]+", " ", value.casefold())
    terms = set()

    for word in normalized.split():
        if len(word) <= 1:
            continue

        terms.add(word)

        if len(word) > 3 and word.endswith("ies"):
            terms.add(word[:-3] + "y")
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            terms.add(word[:-1])

    return terms


class ToolDiscovery:
    def __init__(self, tools=TOOLS):
        self._tools = tuple(tools)

    def discover(
        self,
        query: str,
        capability: str | None = None,
        include_mutations: bool = False,
        limit: int = 8,
    ) -> list[DiscoveryResult]:
        if type(query) is not str or not query.strip() or len(query) > 500:
            raise ValueError("invalid_query")
        if capability is not None and (
            type(capability) is not str or len(capability) > 100
        ):
            raise ValueError("invalid_capability")
        if type(include_mutations) is not bool:
            raise ValueError("invalid_mutation_filter")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid_limit")

        wanted = _terms(query)
        results = []

        for tool in self._tools:
            if not tool.enabled:
                continue
            if capability and tool.capability != capability:
                continue
            if not include_mutations and (
                tool.approval_required or tool.risk != "read"
            ):
                continue

            access_terms = (
                "read only read-only read_only"
                if tool.risk == "read" and not tool.approval_required
                else "mutation requires approval"
            )
            searchable = (
                f"{tool.name} {tool.description} "
                f"{tool.capability} {' '.join(tool.arguments)} "
                f"{tool.risk} {access_terms}"
            )
            available = _terms(searchable)
            matched = wanted & available
            score = len(matched)

            if capability:
                score += 5
            if tool.name.lower().replace("_", " ") in query.lower():
                score += 4

            if score:
                results.append(
                    DiscoveryResult(
                        tool=tool,
                        score=score,
                        matched_terms=tuple(sorted(matched)),
                    )
                )

        results.sort(key=lambda item: (-item.score, item.tool.name))
        return results[:limit]
