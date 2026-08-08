"""Classify Kalshi sports event families for filtered analysis.

Kalshi's ``market_type`` describes the contract structure (usually ``binary``),
not the sportsbook-style market category.  The stable signal is the event
family: the part of ``event_ticker`` before the first ``-``, with a leading
``KX`` removed.  This module keeps the Python and DuckDB classifiers backed by
the same ordered, RE2-compatible regular expressions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

SPORT_LABELS: dict[str, str] = {
    "nfl": "NFL",
    "nba": "NBA",
    "mlb": "MLB",
    "ncaa_football": "NCAA Football",
    "ncaa_basketball": "NCAA Basketball",
    "nhl": "NHL",
    "wnba": "WNBA",
    "tennis": "Tennis",
    "golf": "Golf",
    "soccer": "Soccer",
    "ufc_boxing": "UFC/Boxing",
    "racing": "Racing",
    "esports": "Esports",
    "other_sports": "Other Sports",
}

MARKET_CATEGORY_LABELS: dict[str, str] = {
    "all": "All",
    "moneyline": "Moneyline",
    "spread": "Spread",
    "score_total": "Score Total",
    "player_props": "Player Props",
    "team_props": "Team Props",
    "futures": "Futures",
}


def _selector_token(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _catalog_aliases(labels: dict[str, str], aliases: dict[str, str]) -> dict[str, str]:
    result = {_selector_token(key, name="catalog key"): key for key in labels}
    result.update(
        {
            _selector_token(label, name="catalog label"): key
            for key, label in labels.items()
        }
    )
    result.update(aliases)
    return result


_SPORT_ALIASES = _catalog_aliases(
    SPORT_LABELS,
    {
        "profootball": "nfl",
        "nationalfootballleague": "nfl",
        "probasketball": "nba",
        "nationalbasketballassociation": "nba",
        "baseball": "mlb",
        "majorleaguebaseball": "mlb",
        "ncaaf": "ncaa_football",
        "collegefootball": "ncaa_football",
        "ncaab": "ncaa_basketball",
        "ncaamb": "ncaa_basketball",
        "collegebasketball": "ncaa_basketball",
        "hockey": "nhl",
        "mma": "ufc_boxing",
        "ufc": "ufc_boxing",
        "boxing": "ufc_boxing",
        "motorsports": "racing",
        "esport": "esports",
        "competitivegaming": "esports",
        "other": "other_sports",
    },
)

_MARKET_CATEGORY_ALIASES = _catalog_aliases(
    MARKET_CATEGORY_LABELS,
    {
        "ml": "moneyline",
        "gamewinner": "moneyline",
        "winner": "moneyline",
        "spreads": "spread",
        "total": "score_total",
        "totals": "score_total",
        "overunder": "score_total",
        "playerprop": "player_props",
        "teamprop": "team_props",
        "gameprop": "team_props",
        "gameprops": "team_props",
        "future": "futures",
        "outright": "futures",
        "outrights": "futures",
    },
)


def _normalize_choice(
    value: str,
    *,
    name: str,
    labels: dict[str, str],
    aliases: dict[str, str],
) -> str:
    token = _selector_token(value, name=name)
    try:
        return aliases[token]
    except KeyError as exc:
        choices = ", ".join(labels.values())
        raise ValueError(f"Unknown {name} {value!r}. Choose one of: {choices}") from exc


def normalize_sport(value: str) -> str:
    """Return a canonical sport key from a key, label, or common alias."""

    return _normalize_choice(
        value,
        name="sport",
        labels=SPORT_LABELS,
        aliases=_SPORT_ALIASES,
    )


def normalize_market_category(value: str) -> str:
    """Return a canonical market-category key from a label or common alias."""

    return _normalize_choice(
        value,
        name="market category",
        labels=MARKET_CATEGORY_LABELS,
        aliases=_MARKET_CATEGORY_ALIASES,
    )


_TENNIS_PREFIX = (
    r"(?:ATP|WTA|DAVISCUP|WMENSINGLES|WWOMENSINGLES|USOMENSINGLES|"
    r"USOWOMENSINGLES|FOMENSINGLES|FOWOMENSINGLES|FOMEN|FOWOMEN)"
)
_GOLF_PREFIX = r"(?:PGA|MASTERS|USOPEN|THEOPEN|GENESISINVITATIONAL|LIV)"
_SOCCER_PREFIX = (
    r"(?:EPL|PREMIERLEAGUE|UCL|UEFACL|LALIGA|SERIEA|BUNDESLIGA|"
    r"LIGUE1|MLS|NWSL|FIFA|CLUBWC|EFLCHAMPIONSHIP|EFLCUP|UEL|"
    r"SUPERLIG|EREDIVISIE|LIGAPORTUGAL|BRASILEIRO|MENWORLDCUP|BALLONDOR)"
)
_ESPORTS_PREFIX = r"(?:COD|CS2|CSGO|DOTA2|LOL|OW|R6|VALORANT)"

# Every pattern is anchored to the complete normalized family.  WNBA is kept
# before NBA deliberately: future rule edits must not reintroduce the historic
# WNBAGAME -> NBA substring collision.
_SPORT_RULES: tuple[tuple[str, str], ...] = (
    ("wnba", r"^WNBA[A-Z0-9]*$"),
    ("nfl", r"^(?:NFL[A-Z0-9]*|MVENFL[A-Z0-9]*|NFC|AFC|SB)$"),
    ("nba", r"^(?:NBA[A-Z0-9]*|MVENBA[A-Z0-9]*)$"),
    ("mlb", r"^MLB[A-Z0-9]*$"),
    ("ncaa_football", r"^(?:NCAAF|HEISMAN)[A-Z0-9]*$"),
    (
        "ncaa_basketball",
        r"^(?:NCAAMB|NCAAWB|MARMAD|WMARMAD)[A-Z0-9]*$",
    ),
    ("nhl", r"^NHL[A-Z0-9]*$"),
    ("tennis", rf"^{_TENNIS_PREFIX}[A-Z0-9]*$"),
    ("golf", rf"^{_GOLF_PREFIX}[A-Z0-9]*$"),
    ("soccer", rf"^{_SOCCER_PREFIX}[A-Z0-9]*$"),
    ("ufc_boxing", r"^(?:UFC|BOXING)[A-Z0-9]*$"),
    ("racing", r"^(?:F1|NASCAR|INDYCAR|INDY500)[A-Z0-9]*$"),
    (
        "esports",
        rf"^(?:{_ESPORTS_PREFIX}[A-Z0-9]*|IEM[A-Z0-9]*|LEAGUEWORLDS|"
        r"INTERNETINVITATIONAL|MVESPORTS[A-Z0-9]*)$",
    ),
    ("other_sports", r"^(?:NATHAN|EUROLEAGUE)[A-Z0-9]*$"),
)

_MIXED_FAMILY_PATTERN = r"^MVE[A-Z0-9]*$"

# Precedence is part of the public behavior.  In particular:
#   futures -> team props -> spread -> total -> player props -> moneyline.
# MVE families are handled before this table and remain unclassified because a
# single parlay can contain legs from several of these categories.
_MARKET_FAMILY_RULES: tuple[tuple[str, str], ...] = (
    # Futures: season, tournament, award, roster, and advancement markets.
    (
        "futures",
        r"^[A-Z0-9]*(?:WINS|MVP|ROTY|POTY|COTY|PLAYOFF|STAGEOFELIM|"
        r"DRAFT|REGTOP|APRANK|TOPAPRANK|ADVANCE|FINALS|FINALSQUAL|"
        r"TOURNWIN|NEXTTEAM|NEXTCONTRACT|TRADE|ALLTEAM|ALLDEFENSE|"
        r"SERIES|HRDERBY)[A-Z0-9]*$",
    ),
    ("futures", r"^[A-Z0-9]*CHAMP(?:ION|IONS|IONSHIP)?$"),
    (
        "futures",
        r"^(?:NFC|AFC|SB|HEISMAN|BALLONDOR|WMENSINGLES|WWOMENSINGLES|"
        r"USOMENSINGLES|USOWOMENSINGLES|FOMENSINGLES|FOWOMENSINGLES|"
        r"FOMEN|FOWOMEN|MASTERS|USOPEN|THEOPEN|GENESISINVITATIONAL|"
        r"LIVTOUR|PGATOUR|PGARYDER|MENWORLDCUP)$",
    ),
    (
        "futures",
        r"^(?:CS2|VALORANT|LEAGUEWORLDS|IEM[A-Z0-9]*)$",
    ),
    (
        "futures",
        r"^(?:NFL(?:NFC|AFC)(?:WEST|EAST|NORTH|SOUTH)|NBA(?:EAST|WEST)|"
        r"NHL(?:EAST|WEST)|MLB(?:AL|NL)(?:EAST|WEST|CENTRAL)?)$",
    ),
    ("futures", r"^(?:UCL|UEL)[A-Z0-9]*ROUND[A-Z0-9]*$"),
    (
        "futures",
        rf"^(?:NFLMATCHUP[A-Z0-9]*|{_SOCCER_PREFIX}[A-Z0-9]*"
        r"(?:TOP4|TEAMPOINTS)[A-Z0-9]*)$",
    ),
    # Non-player game and team props. TEAMTOTAL intentionally precedes TOTAL.
    ("team_props", r"^[A-Z0-9]*TEAMTOTAL[A-Z0-9]*$"),
    (
        "team_props",
        rf"^{_SOCCER_PREFIX}[A-Z0-9]*(?:BTTS|SCORE)[A-Z0-9]*$",
    ),
    (
        "team_props",
        r"^(?:NBAH2HBENCHPTS|NASCARTOPTEAM|MLBRFI|MLBEXTRAS|"
        r"NFLGAMESPECIALS|NFLTSPEC|PGARYDERCUPD1)$",
    ),
    ("spread", r"^[A-Z0-9]*SPREAD[A-Z0-9]*$"),
    ("score_total", r"^[A-Z0-9]*TOTAL[A-Z0-9]*$"),
    # Player-stat and participant-specific props, before winner/moneyline rules.
    (
        "player_props",
        r"^NFL(?:2TD|ANYTD|FIRSTTD|PASSYDS|REC|RECYDS|RSHYDS)[A-Z0-9]*$",
    ),
    (
        "player_props",
        r"^(?:NBA|WNBA)(?:2D|3D|3PT|AST|BLK|PTS|PTSLEADER|REB|STL|PRA)"
        r"[A-Z0-9]*$",
    ),
    (
        "player_props",
        r"^MLB(?:HIT|HILT|HR|HRLR|HRR|KS|OUTS|RBI|SB|TB)[A-Z0-9]*$",
    ),
    (
        "player_props",
        r"^NHL(?:AST|FIRSTGOAL|GOAL|PTS)[A-Z0-9]*$",
    ),
    ("player_props", r"^(?:ATP|WTA)(?:ACES|EXACTMATCH)[A-Z0-9]*$"),
    (
        "player_props",
        rf"^{_SOCCER_PREFIX}[A-Z0-9]*(?:FIRSTGOAL|GOAL)[A-Z0-9]*$",
    ),
    (
        "player_props",
        rf"^{_GOLF_PREFIX}[A-Z0-9]*(?:BOGEYFREE|HOLESCORE|MAKECUT|ROUND|"
        r"TOP|LEADER)[A-Z0-9]*$",
    ),
    (
        "player_props",
        r"^(?:F1|NASCAR|INDYCAR)[A-Z0-9]*(?:FASTESTLAP|RACEPODIUM|"
        r"PODIUM|POLE|TOP20)[A-Z0-9]*$",
    ),
    (
        "player_props",
        r"^(?:BOXING(?:DISTANCE|KNOCKOUT)|UFC(?:VICROUND|MOV))[A-Z0-9]*$",
    ),
    # Exact single-game, match, fight, race, H2H, and three-ball winners.
    ("moneyline", r"^NFLGAME$"),
    (
        "moneyline",
        r"^(?:NBA|WNBA)(?:GAME|(?:SUMMER)?(?:1H|2H|[1-4]Q)WINNER)$",
    ),
    ("moneyline", r"^MLB(?:GAME|F3|F5|ASGAME)$"),
    ("moneyline", r"^NHLGAME$"),
    ("moneyline", r"^NCAAFGAME$"),
    (
        "moneyline",
        r"^(?:NCAAMB|NCAAWB)(?:GAME|D3GAME|(?:1H|2H|[1-4]Q)WINNER)$",
    ),
    (
        "moneyline",
        r"^(?:(?:ATP|WTA)(?:CHALLENGER)?MATCH|(?:ATP|WTA)DOUBLES|"
        r"(?:ATP|WTA)S[0-9]+GWINNER|(?:ATP|WTA)SETWINNER|DAVISCUPMATCH)$",
    ),
    ("moneyline", r"^(?:UFCFIGHT|BOXING)$"),
    ("moneyline", r"^(?:F1RACE|NASCARRACE|INDYCARRACE|NASCARH2H)$"),
    ("moneyline", r"^(?:PGAH2H|PGA3BALL|PGARYDERMATCH)$"),
    ("moneyline", rf"^{_SOCCER_PREFIX}[A-Z0-9]*GAME$"),
    ("moneyline", r"^EUROLEAGUEGAME$"),
    (
        "moneyline",
        r"^(?:COD(?:GAME|MAP)|CS2(?:GAME|MAP)|CSGOGAME|"
        r"DOTA2(?:GAME|MAP)|LOL(?:GAME|GAMES|MAP)|OWGAME|R6GAME|"
        r"VALORANT(?:GAME|MAP))$",
    ),
)

# Fallbacks only run for an otherwise unknown family.  They intentionally use
# high-confidence wording rather than attempting to infer a player from a name.
_MARKET_TEXT_RULES: tuple[tuple[str, str], ...] = (
    (
        "futures",
        r"(^|[^A-Z0-9])(?:MVP|ROOKIE OF THE YEAR|CHAMPIONSHIP|MAKE THE "
        r"PLAYOFFS|REGULAR SEASON WINS|TOURNAMENT WINNER)([^A-Z0-9]|$)",
    ),
    (
        "team_props",
        r"(^|[^A-Z0-9])(?:TEAM TOTAL|BOTH TEAMS TO SCORE|EXACT SCORE)"
        r"([^A-Z0-9]|$)",
    ),
    ("spread", r"(^|[^A-Z0-9])SPREAD([^A-Z0-9]|$)"),
    (
        "score_total",
        r"(^|[^A-Z0-9])(?:OVER/UNDER|TOTAL (?:POINTS|RUNS|GOALS))"
        r"([^A-Z0-9]|$)",
    ),
    (
        "player_props",
        r"(^|[^A-Z0-9])(?:PASSING|RUSHING|RECEIVING) YARDS"
        r"([^A-Z0-9]|$)",
    ),
    (
        "moneyline",
        r"(^|[^A-Z0-9])(?:WHO WILL WIN|GAME WINNER|MATCH WINNER|"
        r"FIGHT WINNER|RACE WINNER)([^A-Z0-9]|$)",
    ),
)

_COMPILED_SPORT_RULES = tuple(
    (key, re.compile(pattern)) for key, pattern in _SPORT_RULES
)
_COMPILED_MARKET_FAMILY_RULES = tuple(
    (key, re.compile(pattern)) for key, pattern in _MARKET_FAMILY_RULES
)
_COMPILED_MARKET_TEXT_RULES = tuple(
    (key, re.compile(pattern)) for key, pattern in _MARKET_TEXT_RULES
)
_COMPILED_MIXED_FAMILY_PATTERN = re.compile(_MIXED_FAMILY_PATTERN)


def _event_family(event_ticker: str | None) -> str:
    if not isinstance(event_ticker, str):
        return ""
    family = event_ticker.strip().upper().split("-", 1)[0]
    if family.startswith("KX"):
        family = family[2:]
    return family


def _first_match(
    value: str, rules: Iterable[tuple[str, re.Pattern[str]]]
) -> str | None:
    for key, pattern in rules:
        if pattern.search(value):
            return key
    return None


def classify_sport(event_ticker: str | None) -> str | None:
    """Classify an event ticker into a canonical sport key, if recognized."""

    family = _event_family(event_ticker)
    if not family:
        return None
    return _first_match(family, _COMPILED_SPORT_RULES)


def classify_market_category(
    event_ticker: str | None,
    title: str | None = None,
    yes_sub_title: str | None = None,
    no_sub_title: str | None = None,
) -> str | None:
    """Classify a sports event into one of the specific market categories.

    Unknown and mixed MVE families return ``None`` so callers do not silently
    include them in a misleading category. ``all`` is a filter option rather
    than a classification result.
    """

    family = _event_family(event_ticker)
    if _COMPILED_MIXED_FAMILY_PATTERN.search(family):
        return None

    category = _first_match(family, _COMPILED_MARKET_FAMILY_RULES)
    if category is not None:
        return category

    text = " ".join(
        value.strip().upper()
        for value in (title, yes_sub_title, no_sub_title)
        if isinstance(value, str) and value.strip()
    )
    if not text:
        return None
    return _first_match(text, _COMPILED_MARKET_TEXT_RULES)


_SQL_IDENTIFIER_PART = r'(?:[A-Za-z_][A-Za-z0-9_]*|"(?:[^"]|"")+")'
_SQL_COLUMN_REFERENCE = re.compile(
    rf"^{_SQL_IDENTIFIER_PART}(?:\.{_SQL_IDENTIFIER_PART})*$"
)
_SQL_STRING_LITERAL = re.compile(r"^'(?:[^']|'')*'$")


def _safe_sql_expression(expression: str, *, name: str) -> str:
    """Allow only a column reference, NULL, or a SQL string literal.

    The builders need SQL expressions rather than data values.  Restricting
    their inputs prevents an interactive selector or other untrusted value from
    being turned into executable SQL accidentally.
    """

    if not isinstance(expression, str) or not expression.strip():
        raise ValueError(f"{name} must be a SQL column reference or string literal")
    expression = expression.strip()
    if (
        _SQL_COLUMN_REFERENCE.fullmatch(expression)
        or _SQL_STRING_LITERAL.fullmatch(expression)
        or expression.upper() == "NULL"
    ):
        return expression
    raise ValueError(
        f"Unsafe {name}: expected a column reference, NULL, or SQL string literal"
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _family_sql(event_ticker_sql: str) -> str:
    event_ticker_sql = _safe_sql_expression(event_ticker_sql, name="event_ticker_sql")
    normalized = f"upper(trim(coalesce(cast(({event_ticker_sql}) AS VARCHAR), '')))"
    before_dash = f"regexp_extract({normalized}, '^([^-]+)', 1)"
    return f"regexp_replace({before_dash}, '^KX', '')"


def _case_sql(
    value_sql: str,
    rules: Iterable[tuple[str, str]],
    *,
    leading_when: Iterable[tuple[str, str]] = (),
) -> str:
    clauses = ["CASE"]
    clauses.extend(
        f"    WHEN regexp_matches({value_sql}, {_sql_literal(pattern)}) "
        f"THEN {_sql_literal(result)}"
        for result, pattern in leading_when
    )
    clauses.extend(
        f"    WHEN regexp_matches({value_sql}, {_sql_literal(pattern)}) "
        f"THEN {_sql_literal(result)}"
        for result, pattern in rules
    )
    clauses.append("    ELSE NULL")
    clauses.append("END")
    return "\n".join(clauses)


def sport_case_sql(event_ticker_sql: str) -> str:
    """Build a null-safe DuckDB CASE expression equivalent to classify_sport."""

    return _case_sql(_family_sql(event_ticker_sql), _SPORT_RULES)


def market_category_case_sql(
    event_ticker_sql: str,
    title_sql: str = "''",
    yes_sub_title_sql: str = "''",
    no_sub_title_sql: str = "''",
) -> str:
    """Build the DuckDB CASE equivalent of classify_market_category."""

    family_sql = _family_sql(event_ticker_sql)
    title_sql = _safe_sql_expression(title_sql, name="title_sql")
    yes_sub_title_sql = _safe_sql_expression(
        yes_sub_title_sql, name="yes_sub_title_sql"
    )
    no_sub_title_sql = _safe_sql_expression(no_sub_title_sql, name="no_sub_title_sql")
    text_sql = (
        "upper(trim(concat("
        f"coalesce(cast(({title_sql}) AS VARCHAR), ''), ' ', "
        f"coalesce(cast(({yes_sub_title_sql}) AS VARCHAR), ''), ' ', "
        f"coalesce(cast(({no_sub_title_sql}) AS VARCHAR), '')"
        ")))"
    )

    clauses = [
        "CASE",
        f"    WHEN regexp_matches({family_sql}, "
        f"{_sql_literal(_MIXED_FAMILY_PATTERN)}) THEN NULL",
    ]
    clauses.extend(
        f"    WHEN regexp_matches({family_sql}, {_sql_literal(pattern)}) "
        f"THEN {_sql_literal(result)}"
        for result, pattern in _MARKET_FAMILY_RULES
    )
    clauses.extend(
        f"    WHEN regexp_matches({text_sql}, {_sql_literal(pattern)}) "
        f"THEN {_sql_literal(result)}"
        for result, pattern in _MARKET_TEXT_RULES
    )
    clauses.extend(("    ELSE NULL", "END"))
    return "\n".join(clauses)


__all__ = [
    "MARKET_CATEGORY_LABELS",
    "SPORT_LABELS",
    "classify_market_category",
    "classify_sport",
    "market_category_case_sql",
    "normalize_market_category",
    "normalize_sport",
    "sport_case_sql",
]
