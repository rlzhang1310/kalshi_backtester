from __future__ import annotations

import duckdb
import pytest

from src.analysis.kalshi.util.sports_markets import (
    MARKET_CATEGORY_LABELS,
    SPORT_LABELS,
    classify_market_category,
    classify_sport,
    market_category_case_sql,
    normalize_market_category,
    normalize_sport,
    sport_case_sql,
)


def test_public_catalogs_contain_only_selectable_values() -> None:
    assert list(SPORT_LABELS) == [
        "nfl",
        "nba",
        "mlb",
        "ncaa_football",
        "ncaa_basketball",
        "nhl",
        "wnba",
        "tennis",
        "golf",
        "soccer",
        "ufc_boxing",
        "racing",
        "esports",
        "other_sports",
    ]
    assert list(MARKET_CATEGORY_LABELS) == [
        "all",
        "moneyline",
        "spread",
        "score_total",
        "player_props",
        "team_props",
        "futures",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("NFL", "nfl"),
        ("ncaa-football", "ncaa_football"),
        ("College Basketball", "ncaa_basketball"),
        ("UFC-Boxing", "ufc_boxing"),
        ("motor sports", "racing"),
        ("competitive gaming", "esports"),
        ("e-sport", "esports"),
        ("other", "other_sports"),
    ],
)
def test_normalize_sport_accepts_labels_keys_and_aliases(
    value: str, expected: str
) -> None:
    assert normalize_sport(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("All", "all"),
        ("Moneyline", "moneyline"),
        ("spreads", "spread"),
        ("score-total", "score_total"),
        ("over/under", "score_total"),
        ("player prop", "player_props"),
        ("game props", "team_props"),
        ("outright", "futures"),
    ],
)
def test_normalize_market_category_accepts_common_terminal_input(
    value: str, expected: str
) -> None:
    assert normalize_market_category(value) == expected


def test_normalizers_reject_unknown_and_empty_choices() -> None:
    with pytest.raises(ValueError, match="Unknown sport"):
        normalize_sport("crystal ball")
    with pytest.raises(ValueError, match="non-empty"):
        normalize_market_category("  ")


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("KXNFLGAME-25SEP07CINCLE", "nfl"),
        ("KXNBAGAME-25NOV04PHXGSW", "nba"),
        ("KXMLBGAME-26MAY251540NYYKC", "mlb"),
        ("KXNCAAFGAME-26SEP01", "ncaa_football"),
        ("KXHEISMAN-26", "ncaa_football"),
        ("KXNCAAMBGAME-25NOV23USTPORT", "ncaa_basketball"),
        ("KXNCAAWBGAME-26MAR01", "ncaa_basketball"),
        ("KXNHLGAME-26MAR22CBJNYI", "nhl"),
        ("KXWNBAGAME-26JUN01", "wnba"),
        ("KXATPMATCH-25NOV02ALTRIN", "tennis"),
        ("KXPGARYDERMATCH-25SEP26", "golf"),
        ("KXEPLGAME-26MAY13MCICRY", "soccer"),
        ("KXUFCFIGHT-26AUG07", "ufc_boxing"),
        ("KXF1RACE-26AUG07", "racing"),
        ("KXLOLGAME-26JUL01T1GEN", "esports"),
        ("KXCS2-IEMRIO26", "esports"),
        ("KXCS2GAME-26JUL01FAVIT", "esports"),
        ("KXDOTA2MAP-26JUL01LIQSPIRIT", "esports"),
        ("KXVALORANT-MASTLD26", "esports"),
        ("KXVALORANTGAME-26JUL01SENNRG", "esports"),
        ("KXOWGAME-26JUL01FLCVAR", "esports"),
        ("KXMVESPORTSMULTIGAMEEXTENDED-S2026ABC", "esports"),
        ("KXNATHANDOGS-26", "other_sports"),
        ("KXEUROLEAGUEGAME-26APR01", "other_sports"),
    ],
)
def test_classify_sport_catalog(ticker: str, expected: str) -> None:
    assert classify_sport(ticker) == expected


def test_sport_classification_uses_exact_family_not_substrings() -> None:
    assert classify_sport("kxwnbagame-26jun01-nyl") == "wnba"
    assert classify_sport("KXMVENFLSINGLEGAME-26SEP01") == "nfl"
    assert classify_sport("KXPRESNBA-26") is None
    assert classify_sport("KXPRESCS2-26") is None
    assert classify_sport(None) is None


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        ("KXNBAGAME-25NOV04PHXGSW-PHX", "moneyline"),
        ("KXATPDOUBLES-25NOV16HELPATSALSKU-HELPAT", "moneyline"),
        ("KXNBASPREAD-25NOV07CHIMIL-MIL7", "spread"),
        ("KXATPGSPREAD-26JUL01AUGPRI-AUG10", "spread"),
        ("KXNBATOTAL-25NOV02UTACHA-252", "score_total"),
        ("KXATPGTOTAL-26JUN29KWONLAN", "score_total"),
        ("KXNBAPTS-26MAR12WASORL-WASBCOULIBALY0-10", "player_props"),
        ("KXNHLFIRSTGOAL-26MAR22CBJNYI-CBJSMONAHAN23", "player_props"),
        ("KXNBATEAMTOTAL-26APR06PHISAS-SAS122", "team_props"),
        ("KXBRASILEIROTEAMTOTAL-26JUL29VITPAL-PAL6", "team_props"),
        ("KXNFLWINS-27JAC-9", "futures"),
        ("KXWNBAMVP-26-OMILES5", "futures"),
        ("KXCS2GAME-26JUL01FAVIT-FA", "moneyline"),
        ("KXDOTA2MAP-26JUL01LIQSPIRIT-2-LIQ", "moneyline"),
        ("KXLOLTOTALMAPS-26JUL01T1GEN-3", "score_total"),
        ("KXLEAGUEWORLDS-26-T1", "futures"),
        ("KXVALORANT-MASTLD26-XLG", "futures"),
    ],
)
def test_classify_each_market_category(ticker: str, expected: str) -> None:
    assert classify_market_category(ticker) == expected


@pytest.mark.parametrize(
    ("ticker", "expected"),
    [
        # Futures must beat the TEAM token and generic winner-like wording.
        ("KXLALIGATEAMPOINTS-27-RMA", "futures"),
        ("KXNFLMATCHUP-27AFC-MIANYJ", "futures"),
        ("KXMLBSERIESEXACT-26-NYYLAD", "futures"),
        # Team totals and exact scores are not generic score totals.
        ("KXNBATEAMTOTAL-26APR06PHISAS-SAS122", "team_props"),
        ("KXUELSCORE-26MAY01", "team_props"),
        # Participant props must beat broader race/match winner families.
        ("KXATPEXACTMATCH-26JUN29KWONLAN", "player_props"),
        ("KXF1RACEPODIUM-26AUG07", "player_props"),
        ("KXPGARYDERCUPD1-26SEP25", "team_props"),
        # 'Championship' is the league name here, not a season future.
        ("KXEFLCHAMPIONSHIPGAME-26APR01", "moneyline"),
    ],
)
def test_ordered_collision_rules(ticker: str, expected: str) -> None:
    assert classify_market_category(ticker) == expected


def test_mve_and_unknown_families_remain_unclassified() -> None:
    assert classify_market_category("KXMVENFLSINGLEGAME-26SEP01") is None
    assert classify_market_category("KXMVESPORTSMULTIGAMEEXTENDED-S2026ABC") is None
    assert (
        classify_market_category(
            "KXMVENFLSINGLEGAME-26SEP01",
            title="Game winner",
        )
        is None
    )
    assert classify_market_category("KXUNKNOWNSPORT-26") is None
    assert classify_market_category(None) is None


@pytest.mark.parametrize(
    ("title", "yes_sub_title", "expected"),
    [
        ("Will the game spread be 6.5?", "", "spread"),
        ("Both teams to score", "Yes", "team_props"),
        ("Total points", "Over 210.5", "score_total"),
        ("Who will win?", "Home", "moneyline"),
    ],
)
def test_conservative_text_fallback_for_unknown_families(
    title: str, yes_sub_title: str, expected: str
) -> None:
    assert (
        classify_market_category(
            "KXNEWFAMILY-26",
            title=title,
            yes_sub_title=yes_sub_title,
        )
        == expected
    )


def test_duckdb_case_builders_match_python_classifiers() -> None:
    rows = [
        ("KXWNBAGAME-26JUN01", "Game winner", "New York", "Chicago"),
        ("KXNBATEAMTOTAL-26APR06PHISAS", "", "", ""),
        ("KXNFLWINS-27JAC", "", "", ""),
        ("KXF1RACEPODIUM-26AUG07", "", "", ""),
        ("KXMVENFLSINGLEGAME-26SEP01", "Game winner", "", ""),
        ("KXNEWFAMILY-26", "Both teams to score", "", ""),
        ("KXCS2GAME-26JUL01FAVIT", "Match winner", "FaZe", "Vitality"),
        ("KXLEAGUEWORLDS-26", "World champion", "T1", "Other"),
        (None, None, None, None),
    ]
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE examples (
            event_ticker VARCHAR,
            title VARCHAR,
            yes_sub_title VARCHAR,
            no_sub_title VARCHAR
        )
        """
    )
    connection.executemany("INSERT INTO examples VALUES (?, ?, ?, ?)", rows)

    result = connection.execute(
        f"""
        SELECT
            {sport_case_sql("examples.event_ticker")} AS sport,
            {
            market_category_case_sql(
                "examples.event_ticker",
                "examples.title",
                "examples.yes_sub_title",
                "examples.no_sub_title",
            )
        } AS category
        FROM examples
        ORDER BY rowid
        """
    ).fetchall()

    expected = [
        (
            classify_sport(event_ticker),
            classify_market_category(event_ticker, title, yes, no),
        )
        for event_ticker, title, yes, no in rows
    ]
    assert result == expected


def test_sql_builders_reject_executable_input() -> None:
    with pytest.raises(ValueError, match="Unsafe event_ticker_sql"):
        sport_case_sql("event_ticker); DROP TABLE markets; --")
    with pytest.raises(ValueError, match="Unsafe title_sql"):
        market_category_case_sql("event_ticker", "upper(title)")
