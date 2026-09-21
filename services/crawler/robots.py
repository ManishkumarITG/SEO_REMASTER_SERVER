"""robots.txt parsing and path matching.

`parse_robots` moved here from fetcher.py. For its existing caller (the audit
fetcher) the behaviour is unchanged: the `*` group's Disallow/Allow lines and
every Sitemap directive land in the same `info` dict keys as before. It is
extended additively in two ways the catalog crawler needs:

- a group that names our own bot wins over `*`, as the standard says it should;
- `Crawl-delay` is kept (it used to be dropped on the floor).

`path_allowed` implements the matching the fetcher never did. Shopify's stock
robots.txt uses wildcards (`Disallow: /*?*oseid=*`), so a raw prefix comparison
would be theatre - this handles `*` and a trailing `$` anchor, longest match
wins, and Allow beats Disallow on a tie (Google's documented rule).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

#: Case-insensitive substring matched against `User-agent:` lines. Kept as our
#: own token on purpose: several stores hand `Crawl-delay: 10` to named SEO bots
#: (AhrefsBot, MJ12bot) while leaving `*` unthrottled. Impersonating one of them
#: would inherit their throttle.
USER_AGENT_TOKEN = "seoanalyzerbot"

#: Lines read from robots.txt. A multi-megabyte file is a trap, not a config.
MAX_LINES = 500


def parse_robots(text: str, info: dict[str, Any]) -> None:
    """Fill `info` with the rules that apply to us.

    `info` must already carry list-valued "disallow", "allow" and "sitemaps"
    keys (the fetcher's skeleton). "crawl_delay" is set when present; callers
    that never look for it are unaffected.

    Groups are collected first, then one agent key is chosen - our own token if
    any group names it, else `*` - and the rules of EVERY group under that key
    are accumulated. Accumulating (rather than taking the first group) preserves
    the previous behaviour for files that declare `User-agent: *` twice.
    """
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    in_rules = False

    for raw_line in text.splitlines()[:MAX_LINES]:
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            if in_rules:
                # A User-agent line after rules starts a new group.
                groups.append((agents, rules))
                agents, rules, in_rules = [], [], False
            agents.append(value.lower())
        elif directive == "sitemap":
            info["sitemaps"].append(value)
        elif directive in ("disallow", "allow", "crawl-delay"):
            in_rules = True
            rules.append((directive, value))
    if agents:
        groups.append((agents, rules))

    own = any(USER_AGENT_TOKEN in agent for group_agents, _ in groups for agent in group_agents)

    def applies(group_agents: list[str]) -> bool:
        if own:
            return any(USER_AGENT_TOKEN in agent for agent in group_agents)
        return "*" in group_agents

    for group_agents, group_rules in groups:
        if not applies(group_agents):
            continue
        for directive, value in group_rules:
            if directive in ("disallow", "allow"):
                if value:
                    info[directive].append(value)
            else:  # crawl-delay
                try:
                    info["crawl_delay"] = float(value)
                except ValueError:
                    pass


@lru_cache(maxsize=512)
def _pattern(rule: str) -> re.Pattern[str]:
    anchored = rule.endswith("$")
    body = rule[:-1] if anchored else rule
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile("^" + regex + ("$" if anchored else ""))


def path_allowed(path: str, disallow: list[str], allow: list[str]) -> bool:
    """True when `path` may be fetched under these rules.

    Longest matching rule wins; on equal length Allow wins. An empty rule set
    allows everything, and a bare `Disallow:` (empty value) never reaches here
    because parse_robots drops it.
    """
    best_len = -1
    allowed = True
    for rule in disallow:
        if len(rule) > best_len and _pattern(rule).match(path):
            best_len, allowed = len(rule), False
    for rule in allow:
        if len(rule) >= best_len and _pattern(rule).match(path):
            best_len, allowed = len(rule), True
    return allowed


@dataclass(slots=True)
class RobotsRules:
    """The parsed rules for one host, as the catalog crawler consumes them."""

    fetched: bool = False
    status_code: int | None = None
    disallow: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    crawl_delay: float | None = None

    @classmethod
    def from_info(cls, info: dict[str, Any]) -> RobotsRules:
        return cls(
            fetched=bool(info.get("fetched")),
            status_code=info.get("status_code"),
            disallow=list(info.get("disallow") or []),
            allow=list(info.get("allow") or []),
            sitemaps=list(info.get("sitemaps") or []),
            crawl_delay=info.get("crawl_delay"),
        )

    @classmethod
    def permissive(cls) -> RobotsRules:
        """What to assume when robots.txt could not be read: no restrictions."""
        return cls()

    def allows(self, path: str) -> bool:
        return path_allowed(path, self.disallow, self.allow)

    @property
    def blanket_disallow(self) -> bool:
        """True when the applicable group disallows everything."""
        return not self.allows("/")
