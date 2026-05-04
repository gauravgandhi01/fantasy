#!/usr/bin/env python3
"""Build a self-contained static dashboard for True League history."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


ROOT = Path(__file__).resolve().parent
LOCAL_ESPN_API = ROOT / "espn-api"
if LOCAL_ESPN_API.exists():
    sys.path.insert(0, str(LOCAL_ESPN_API))

from espn_api.requests.espn_requests import (  # noqa: E402
    ESPNAccessDenied,
    ESPNInvalidLeague,
    ESPNUnknownError,
    EspnFantasyRequests,
)


DEFAULT_LEAGUE_ID = 594599
DEFAULT_OUTPUT = "index.html"
DEFAULT_TITLE = "True League"
DEFAULT_MANAGER_MAP = "manager_mappings.json"
DEFAULT_MANUAL_OVERRIDES = "manual_overrides.json"
DISCOVERY_MIN_YEAR = 2000
MANAGER_COLORS = [
    "#2f5d7c",
    "#3f7d58",
    "#9a5b13",
    "#9f3a4f",
    "#5a4f9f",
    "#24706f",
    "#7b4b2a",
    "#6a6f2b",
    "#476a9f",
    "#8a4f86",
    "#2f6f4f",
    "#a25f2a",
    "#6c4b2f",
    "#4d6575",
    "#7a3f3f",
    "#4f6f32",
]


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def round1(value: Any) -> float:
    return round(safe_float(value), 1)


def pct(wins: int, losses: int, ties: int) -> float:
    games = wins + losses + ties
    if games == 0:
        return 0.0
    return round((wins + (ties * 0.5)) / games, 3)


def record_string(wins: int, losses: int, ties: int) -> str:
    if ties:
        return f"{wins}-{losses}-{ties}"
    return f"{wins}-{losses}"


def team_name(raw_team: dict[str, Any]) -> str:
    name = raw_team.get("name")
    if name and name != "Unknown":
        return str(name)
    location = raw_team.get("location")
    nickname = raw_team.get("nickname")
    fallback = " ".join(str(part) for part in (location, nickname) if part)
    return fallback or f"Team {raw_team.get('id', 'Unknown')}"


def member_display(member: dict[str, Any] | None) -> str:
    if not member:
        return "Unknown Owner"

    first = member.get("firstName") or ""
    last = member.get("lastName") or ""
    full = f"{first} {last}".strip()
    if full:
        return full

    for key in ("displayName", "nickname"):
        value = member.get(key)
        if value:
            return str(value)

    return str(member.get("id") or "Unknown Owner")


def member_name_options(member: dict[str, Any] | None) -> list[dict[str, str]]:
    if not member:
        return []

    options = []
    seen = set()

    def add(field: str, value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if not text or text in seen:
            return
        seen.add(text)
        options.append({"field": field, "value": text})

    first = member.get("firstName")
    last = member.get("lastName")
    add("fullName", f"{first or ''} {last or ''}".strip())
    add("displayName", member.get("displayName"))
    add("nickname", member.get("nickname"))
    add("firstName", first)
    add("lastName", last)
    add("id", member.get("id"))
    return options


def owner_ids(raw_team: dict[str, Any]) -> list[str]:
    owners = raw_team.get("owners") or []
    return sorted(str(owner) for owner in owners if owner)


def manager_key(year: int, team_id: int, owners: list[str]) -> str:
    if owners:
        return "owner:" + "+".join(owners)
    return f"unknown:{year}:{team_id}"


def owner_display_from_ids(ids: list[str], members_by_id: dict[str, dict[str, Any]]) -> str:
    if not ids:
        return "Unknown Owner"
    return " / ".join(member_display(members_by_id.get(owner_id)) for owner_id in ids)


def owner_name_options_from_ids(ids: list[str], members_by_id: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    options = []
    seen = set()
    for owner_id in ids:
        for option in member_name_options(members_by_id.get(owner_id)):
            key = (option["field"], option["value"])
            if key in seen:
                continue
            seen.add(key)
            options.append(option)
    return options


def load_manager_mappings(path: str | None) -> dict[str, Any]:
    if not path:
        return {"manager_keys": {}, "owner_ids": {}, "manager_key_aliases": {}}

    mapping_path = Path(path)
    if not mapping_path.exists():
        return {"manager_keys": {}, "owner_ids": {}, "manager_key_aliases": {}}

    data = json.loads(mapping_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{mapping_path} must contain a JSON object")

    manager_keys = data.get("manager_keys", {})
    owner_id_map = data.get("owner_ids", {})
    manager_key_aliases = data.get("manager_key_aliases", {})

    # Also accept a compact form: {"owner:{...}": "Real Name"}.
    for key, value in data.items():
        if key.startswith(("owner:", "unknown:")) and isinstance(value, str):
            manager_keys[key] = value

    return {
        "manager_keys": {str(key): str(value) for key, value in manager_keys.items() if str(value).strip()},
        "owner_ids": {str(key): str(value) for key, value in owner_id_map.items() if str(value).strip()},
        "manager_key_aliases": {
            str(key): str(value)
            for key, value in manager_key_aliases.items()
            if str(key).strip() and str(value).strip()
        },
    }


def canonical_manager_key(raw_key: str, mappings: dict[str, Any]) -> str:
    alias_map = mappings.get("manager_key_aliases", {})
    if not isinstance(alias_map, dict) or not alias_map:
        return raw_key

    key = str(raw_key)
    seen = {key}
    while key in alias_map:
        next_key = str(alias_map[key])
        if not next_key or next_key in seen:
            break
        key = next_key
        seen.add(key)
    return key


def display_override(
    manager_key_value: str,
    ids: list[str],
    default_display: str,
    mappings: dict[str, Any],
) -> str:
    manager_key_map = mappings.get("manager_keys", {})
    owner_id_map = mappings.get("owner_ids", {})

    if manager_key_value in manager_key_map:
        return manager_key_map[manager_key_value]

    if ids and all(owner_id in owner_id_map for owner_id in ids):
        return " / ".join(owner_id_map[owner_id] for owner_id in ids)

    return default_display


def write_manager_mapping_template(data: dict[str, Any], path: str) -> None:
    template = {
        "manager_keys": {
            manager["manager_key"]: manager["display"]
            for manager in sorted(data["managers"], key=lambda item: item["display"].lower())
        },
        "owner_ids": {},
        "manager_key_aliases": {},
    }

    for manager in data["managers"]:
        if len(manager.get("owner_ids", [])) == 1:
            template["owner_ids"][manager["owner_ids"][0]] = manager["display"]

    Path(path).write_text(json.dumps(template, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def load_manual_overrides(path: str | None) -> dict[str, Any]:
    if not path:
        return {"champions": {}, "matchup_results": []}

    override_path = Path(path)
    if not override_path.exists():
        return {"champions": {}, "matchup_results": []}

    data = json.loads(override_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{override_path} must contain a JSON object")

    champions = data.get("champions", {})
    if isinstance(champions, list):
        champions = {str(item.get("year")): item for item in champions if isinstance(item, dict) and item.get("year")}
    if not isinstance(champions, dict):
        raise ValueError(f"{override_path} champions must be an object or list")

    matchup_results = data.get("matchup_results", [])
    if isinstance(matchup_results, dict):
        matchup_results = list(matchup_results.values())
    if not isinstance(matchup_results, list):
        raise ValueError(f"{override_path} matchup_results must be a list or object")

    return {"champions": champions, "matchup_results": matchup_results}


def row_matches_champion_override(row: dict[str, Any], override: dict[str, Any]) -> bool:
    if override.get("team_ref") and row.get("team_ref") == override["team_ref"]:
        return True
    if override.get("manager_key") and row.get("manager_key") == override["manager_key"]:
        return True
    if override.get("team_id") is not None and safe_int(row.get("team_id")) == safe_int(override.get("team_id")):
        return True
    if override.get("team_name") and row.get("team_name") == override["team_name"]:
        return True
    return False


def row_matches_matchup_override(matchup: dict[str, Any], override: dict[str, Any]) -> bool:
    if override.get("matchup_ref") and matchup.get("matchup_ref") != override.get("matchup_ref"):
        return False
    if override.get("year") is not None and safe_int(matchup.get("year")) != safe_int(override.get("year")):
        return False
    if override.get("week") is not None and safe_int(matchup.get("week")) != safe_int(override.get("week")):
        return False

    home = matchup.get("home") or {}
    away = matchup.get("away") or {}

    if override.get("home_team_ref") and home.get("team_ref") != override.get("home_team_ref"):
        return False
    if override.get("away_team_ref") and away.get("team_ref") != override.get("away_team_ref"):
        return False
    if override.get("home_team_name") and home.get("team_name") != override.get("home_team_name"):
        return False
    if override.get("away_team_name") and away.get("team_name") != override.get("away_team_name"):
        return False

    team_refs = override.get("team_refs") or []
    if team_refs:
        row_team_refs = sorted([home.get("team_ref"), away.get("team_ref")])
        if sorted(team_refs) != row_team_refs:
            return False

    return True


def normalize_matchup_winner(*, winner: str | None, home_score: float, away_score: float) -> str:
    candidate = (winner or "").upper().strip()
    if candidate in {"HOME", "AWAY", "TIE", "UNDECIDED"}:
        return candidate
    if home_score > away_score:
        return "HOME"
    if away_score > home_score:
        return "AWAY"
    return "TIE"


def apply_matchup_result_overrides(
    matchups: list[dict[str, Any]],
    manual_overrides: dict[str, Any],
) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    matchup_overrides = manual_overrides.get("matchup_results", [])
    if not isinstance(matchup_overrides, list):
        return applied

    for override in matchup_overrides:
        if not isinstance(override, dict):
            continue
        target = next((row for row in matchups if row_matches_matchup_override(row, override)), None)
        if not target:
            applied.append(
                {
                    "type": "matchup_result",
                    "status": "not_found",
                    "target": override,
                }
            )
            continue

        home = target.get("home") or {}
        away = target.get("away") or {}
        home_score = round1(override.get("home_score")) if "home_score" in override else round1(home.get("score"))
        away_score = round1(override.get("away_score")) if "away_score" in override else round1(away.get("score"))
        home["score"] = home_score
        away["score"] = away_score
        target["home"] = home
        target["away"] = away

        winner = normalize_matchup_winner(
            winner=override.get("winner"),
            home_score=home_score,
            away_score=away_score,
        )
        target["winner"] = winner
        target["completed"] = winner != "UNDECIDED"
        target["margin"] = round1(abs(home_score - away_score))
        if winner == "HOME":
            target["winning_side"] = "home"
        elif winner == "AWAY":
            target["winning_side"] = "away"
        elif winner == "TIE":
            target["winning_side"] = "tie"
        else:
            target["winning_side"] = None

        applied.append(
            {
                "type": "matchup_result",
                "status": "applied",
                "matchup_ref": target.get("matchup_ref"),
                "year": target.get("year"),
                "week": target.get("week"),
                "away_team": away.get("team_name"),
                "home_team": home.get("team_name"),
                "away_score": away_score,
                "home_score": home_score,
                "winner": winner,
                "note": override.get("note") or "",
            }
        )

    return applied


def apply_manual_overrides(
    standings: list[dict[str, Any]],
    matchups: list[dict[str, Any]],
    manual_overrides: dict[str, Any],
) -> list[dict[str, Any]]:
    applied = []

    for row in standings:
        row["is_champion"] = row.get("final_rank") == 1
        row["champion_source"] = "espn" if row["is_champion"] else ""
        row["champion_note"] = ""

    champion_overrides = manual_overrides.get("champions", {})
    for year_key, override in champion_overrides.items():
        if not isinstance(override, dict):
            continue
        year = safe_int(override.get("year", year_key))
        candidates = [row for row in standings if row.get("year") == year]
        target = next((row for row in candidates if row_matches_champion_override(row, override)), None)
        if not target:
            applied.append(
                {
                    "type": "champion",
                    "year": year,
                    "status": "not_found",
                    "target": override,
                }
            )
            continue

        for row in candidates:
            row["is_champion"] = row is target
            row["champion_source"] = "manual" if row is target else ""
            row["champion_note"] = override.get("note") or ""

        applied.append(
            {
                "type": "champion",
                "year": year,
                "status": "applied",
                "team_ref": target.get("team_ref"),
                "team_name": target.get("team_name"),
                "manager_key": target.get("manager_key"),
                "note": override.get("note") or "",
            }
        )

    applied.extend(apply_matchup_result_overrides(matchups, manual_overrides))
    return applied


def side_score(side: dict[str, Any]) -> float:
    if "totalPoints" in side:
        return round1(side.get("totalPoints"))
    if "totalPointsLive" in side:
        return round1(side.get("totalPointsLive"))
    return 0.0


def classify_bracket_type(stage: str, playoff_tier: str) -> str:
    """Return regular/playoff/consolation/postseason based on ESPN tier data."""
    if stage != "playoff":
        return "regular"

    tier = (playoff_tier or "NONE").upper()
    if tier == "NONE":
        # ESPN sometimes omits tier details for postseason matchups.
        return "postseason"
    if "CONSOLATION" in tier or "LADDER" in tier or tier.startswith("LOSERS_"):
        return "consolation"
    if "WINNERS_BRACKET" in tier:
        return "playoff"
    return "postseason"


def discover_available_years(
    *,
    league_id: int,
    espn_s2: str,
    swid: str,
    min_year: int = DISCOVERY_MIN_YEAR,
    max_year: int | None = None,
) -> list[int]:
    """Discover all years ESPN allows access to for this league."""
    if max_year is None:
        max_year = datetime.now(timezone.utc).year + 1

    cookies = {"espn_s2": espn_s2, "SWID": swid}
    years: list[int] = []

    for year in range(min_year, max_year + 1):
        request = EspnFantasyRequests(
            sport="nfl",
            year=year,
            league_id=league_id,
            cookies=cookies,
        )
        try:
            data = request.league_get(params={"view": "mSettings"})
        except (ESPNAccessDenied, ESPNInvalidLeague, ESPNUnknownError, requests.RequestException):
            continue
        if isinstance(data, dict) and data.get("settings"):
            years.append(year)

    if not years:
        raise ESPNAccessDenied(
            f"Could not discover any accessible seasons for league {league_id}. "
            "Verify ESPN_S2/SWID and league access."
        )
    return years


def resolve_selected_years(
    *,
    available_years: list[int],
    requested_start: int | None,
    requested_end: int | None,
) -> tuple[int, int, list[int]]:
    min_year = min(available_years)
    max_year = max(available_years)
    start_year = requested_start if requested_start is not None else min_year
    end_year = requested_end if requested_end is not None else max_year

    if start_year > end_year:
        raise ValueError("--end-year must be greater than or equal to --start-year.")

    if start_year < min_year or start_year > max_year:
        raise ValueError(
            f"--start-year {start_year} is outside accessible range {min_year}-{max_year}."
        )
    if end_year < min_year or end_year > max_year:
        raise ValueError(
            f"--end-year {end_year} is outside accessible range {min_year}-{max_year}."
        )

    selected_years = [year for year in available_years if start_year <= year <= end_year]
    if not selected_years:
        raise ValueError(
            f"No accessible seasons in selected range {start_year}-{end_year}. "
            f"Available years: {', '.join(str(year) for year in available_years)}"
        )

    return start_year, end_year, selected_years


def infer_playoff_contenders(teams: list[dict[str, Any]], playoff_team_count: int) -> set[int]:
    contenders = {
        safe_int(team.get("team_id"))
        for team in teams
        if safe_int(team.get("team_id")) > 0
        and safe_int(team.get("playoff_seed")) > 0
        and safe_int(team.get("playoff_seed")) <= playoff_team_count
    }
    if len(contenders) >= 2:
        return contenders

    ranked = sorted(
        (team for team in teams if safe_int(team.get("team_id")) > 0 and safe_int(team.get("final_rank")) > 0),
        key=lambda team: safe_int(team.get("final_rank"), 999),
    )
    return {safe_int(team.get("team_id")) for team in ranked[: max(0, playoff_team_count)]}


def apply_postseason_inference(
    matchups: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    playoff_team_count: int,
) -> None:
    """Infer playoff-vs-consolation when ESPN does not populate playoffTierType."""
    contenders = infer_playoff_contenders(teams, playoff_team_count)
    if len(contenders) < 2:
        return

    playoff_weeks = sorted(
        {
            safe_int(matchup.get("week"))
            for matchup in matchups
            if matchup.get("stage") == "playoff" and safe_int(matchup.get("week")) > 0
        }
    )

    for week in playoff_weeks:
        week_games = [
            matchup
            for matchup in matchups
            if matchup.get("stage") == "playoff"
            and safe_int(matchup.get("week")) == week
            and matchup.get("home")
            and matchup.get("away")
        ]
        if not week_games:
            continue

        next_contenders: set[int] = set()
        played_contenders: set[int] = set()

        for matchup in week_games:
            home_id = safe_int((matchup.get("home") or {}).get("team_id"))
            away_id = safe_int((matchup.get("away") or {}).get("team_id"))
            home_contender = home_id in contenders
            away_contender = away_id in contenders
            if home_contender:
                played_contenders.add(home_id)
            if away_contender:
                played_contenders.add(away_id)

            inferred_type = "consolation"
            if home_contender and away_contender:
                inferred_type = "playoff"
                winner = (matchup.get("winner") or "").upper()
                if winner == "HOME":
                    next_contenders.add(home_id)
                elif winner == "AWAY":
                    next_contenders.add(away_id)
                else:
                    # Keep both if result is unavailable/ambiguous.
                    next_contenders.update([home_id, away_id])

            matchup["bracket_type_inferred"] = inferred_type
            if matchup.get("playoff_tier") == "NONE" and matchup.get("bracket_type") == "postseason":
                matchup["bracket_type"] = inferred_type
                matchup["bracket_source"] = "inferred"
            else:
                matchup["bracket_source"] = "espn_tier"

        byes = contenders - played_contenders
        contenders = next_contenders | byes


def collect_season(
    *,
    league_id: int,
    year: int,
    espn_s2: str,
    swid: str,
    manager_mappings: dict[str, Any],
) -> dict[str, Any]:
    cookies = {"espn_s2": espn_s2, "SWID": swid}
    request = EspnFantasyRequests(
        sport="nfl",
        year=year,
        league_id=league_id,
        cookies=cookies,
    )
    raw = request.league_get(
        params={
            "view": [
                "mTeam",
                "mRoster",
                "mMatchup",
                "mMatchupScore",
                "mSettings",
                "mStandings",
            ]
        }
    )

    settings = raw.get("settings", {})
    schedule_settings = settings.get("scheduleSettings", {})
    status = raw.get("status", {})
    members = raw.get("members", []) or []
    members_by_id = {str(member.get("id")): member for member in members if member.get("id")}

    reg_season_count = safe_int(schedule_settings.get("matchupPeriodCount"))
    playoff_team_count = safe_int(schedule_settings.get("playoffTeamCount"))
    team_count = safe_int(settings.get("size"), len(raw.get("teams", []) or []))
    league_name = settings.get("name") or DEFAULT_TITLE

    season = {
        "year": year,
        "league_name": league_name,
        "team_count": team_count,
        "reg_season_count": reg_season_count,
        "playoff_team_count": playoff_team_count,
        "first_scoring_period": safe_int(status.get("firstScoringPeriod")),
        "final_scoring_period": safe_int(status.get("finalScoringPeriod")),
        "current_matchup_period": safe_int(status.get("currentMatchupPeriod")),
        "scoring_period": safe_int(raw.get("scoringPeriodId")),
        "scoring_type": settings.get("scoringSettings", {}).get("scoringType"),
        "playoff_seed_tie_rule": schedule_settings.get("playoffSeedingRule"),
    }

    teams: list[dict[str, Any]] = []
    standings: list[dict[str, Any]] = []
    team_lookup: dict[int, dict[str, Any]] = {}

    for raw_team in sorted(raw.get("teams", []) or [], key=lambda item: safe_int(item.get("id"))):
        team_id = safe_int(raw_team.get("id"))
        owners = owner_ids(raw_team)
        raw_key = manager_key(year, team_id, owners)
        key = canonical_manager_key(raw_key, manager_mappings)
        default_display = owner_display_from_ids(owners, members_by_id)
        display = display_override(key, owners, default_display, manager_mappings)
        name_options = owner_name_options_from_ids(owners, members_by_id)
        name = team_name(raw_team)
        record = raw_team.get("record", {}).get("overall", {})
        counters = raw_team.get("transactionCounter", {}) or {}

        wins = safe_int(record.get("wins"))
        losses = safe_int(record.get("losses"))
        ties = safe_int(record.get("ties"))
        points_for = round1(record.get("pointsFor"))
        points_against = round1(record.get("pointsAgainst"))
        final_rank = raw_team.get("rankFinal") or raw_team.get("rankCalculatedFinal")
        final_rank = safe_int(final_rank) if final_rank is not None else None

        team_row = {
            "team_ref": f"{year}:{team_id}",
            "year": year,
            "team_id": team_id,
            "manager_key": key,
            "owner_ids": owners,
            "owner_display": display,
            "owner_default_display": default_display,
            "owner_name_options": name_options,
            "team_name": name,
            "team_abbrev": raw_team.get("abbrev") or "",
            "logo_url": raw_team.get("logo") or "",
            "division_id": raw_team.get("divisionId"),
            "playoff_seed": safe_int(raw_team.get("playoffSeed")),
            "final_rank": final_rank,
        }
        team_lookup[team_id] = team_row
        teams.append(team_row)

        standings.append(
            {
                **team_row,
                "is_champion": final_rank == 1,
                "champion_source": "espn" if final_rank == 1 else "",
                "champion_note": "",
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "record": record_string(wins, losses, ties),
                "win_pct": pct(wins, losses, ties),
                "points_for": points_for,
                "points_against": points_against,
                "acquisitions": safe_int(counters.get("acquisitions")),
                "drops": safe_int(counters.get("drops")),
                "trades": safe_int(counters.get("trades")),
                "move_to_ir": safe_int(counters.get("moveToIR")),
                "waiver_rank": safe_int(raw_team.get("waiverRank")),
                "streak_type": record.get("streakType") or "",
                "streak_length": safe_int(record.get("streakLength")),
            }
        )

    matchups: list[dict[str, Any]] = []
    for idx, matchup in enumerate(raw.get("schedule", []) or []):
        week = safe_int(matchup.get("matchupPeriodId"))
        stage = "regular" if reg_season_count and week <= reg_season_count else "playoff"
        playoff_tier = str(matchup.get("playoffTierType") or "NONE")
        bracket_type = classify_bracket_type(stage, playoff_tier)
        winner = matchup.get("winner") or "UNDECIDED"

        def build_side(side_name: str) -> dict[str, Any] | None:
            raw_side = matchup.get(side_name)
            if not raw_side:
                return None
            team_id = safe_int(raw_side.get("teamId"))
            team_row = team_lookup.get(team_id)
            return {
                "team_id": team_id,
                "team_ref": team_row.get("team_ref") if team_row else None,
                "team_name": team_row.get("team_name") if team_row else "Unknown",
                "manager_key": team_row.get("manager_key") if team_row else None,
                "owner_display": team_row.get("owner_display") if team_row else "Unknown Owner",
                "score": side_score(raw_side),
            }

        home = build_side("home")
        away = build_side("away")
        home_score = home["score"] if home else None
        away_score = away["score"] if away else None
        completed = winner != "UNDECIDED"
        margin = None
        winning_side = None

        if home_score is not None and away_score is not None:
            margin = round(abs(home_score - away_score), 1)
            if home_score > away_score:
                winning_side = "home"
            elif away_score > home_score:
                winning_side = "away"
            elif winner == "TIE":
                winning_side = "tie"

        matchups.append(
            {
                "matchup_ref": f"{year}:{week}:{idx + 1}",
                "year": year,
                "week": week,
                "stage": stage,
                "playoff_tier": playoff_tier,
                "bracket_type": bracket_type,
                "bracket_source": "espn_tier" if playoff_tier != "NONE" else "stage",
                "winner": winner,
                "winning_side": winning_side,
                "margin": margin,
                "completed": completed,
                "home": home,
                "away": away,
            }
        )

    apply_postseason_inference(matchups, teams, playoff_team_count)

    standings.sort(
        key=lambda row: (
            row["final_rank"] if row["final_rank"] is not None else 999,
            row["playoff_seed"] or 999,
            -row["wins"],
            -row["points_for"],
        )
    )

    return {
        "season": season,
        "teams": teams,
        "standings": standings,
        "matchups": matchups,
    }


def summarize_managers(
    standings: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    matchups: list[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Enhanced manager summary with avg_rank and playoff record."""
    managers: dict[str, dict[str, Any]] = {}
    latest_year: dict[str, int] = defaultdict(int)

    for team in teams:
        key = team["manager_key"]
        manager = managers.setdefault(
            key,
            {
                "manager_key": key,
                "display": team["owner_display"],
                "default_display": team.get("owner_default_display") or team["owner_display"],
                "name_options": set(),
                "owner_ids": set(),
                "aliases": defaultdict(set),
                "team_refs": [],
                "seasons": set(),
                "championships": 0,
                "top_three": 0,
                "wins": 0,
                "losses": 0,
                "ties": 0,
                "points_for": 0.0,
                "points_against": 0.0,
                "acquisitions": 0,
                "drops": 0,
                "trades": 0,
                "ranks": [],
                "playoff_appearances": 0,
                "playoff_wins": 0,
                "playoff_losses": 0,
                "playoff_ties": 0,
                "playoff_seasons": set(),
            },
        )
        manager["owner_ids"].update(team.get("owner_ids") or [])
        for option in team.get("owner_name_options") or []:
            manager["name_options"].add(f"{option['field']}: {option['value']}")
        manager["aliases"][team["team_name"]].add(team["year"])
        manager["team_refs"].append(team["team_ref"])
        manager["seasons"].add(team["year"])
        if team["year"] >= latest_year[key] and team["owner_display"] != "Unknown Owner":
            manager["display"] = team["owner_display"]
            latest_year[key] = team["year"]

    # Collect ranks and playoff info
    for row in standings:
        key = row["manager_key"]
        if key in managers:
            manager = managers[key]
            if row.get("final_rank") is not None:
                manager["ranks"].append(row["final_rank"])
            manager["championships"] += 1 if row.get("is_champion") else 0
            manager["top_three"] += 1 if row.get("final_rank") in (1, 2, 3) else 0
            manager["wins"] += row["wins"]
            manager["losses"] += row["losses"]
            manager["ties"] += row["ties"]
            manager["points_for"] += row["points_for"]
            manager["points_against"] += row["points_against"]
            manager["acquisitions"] += row["acquisitions"]
            manager["drops"] += row["drops"]
            manager["trades"] += row["trades"]

    # Calculate avg rank
    for manager in managers.values():
        if manager["ranks"]:
            manager["avg_rank"] = round(sum(manager["ranks"]) / len(manager["ranks"]), 2)
        else:
            manager["avg_rank"] = None

    if matchups:
        for matchup in matchups:
            if matchup.get("bracket_type") != "playoff" or not matchup.get("completed"):
                continue
            home = matchup.get("home")
            away = matchup.get("away")
            if not home or not away:
                continue
            home_key = home.get("manager_key")
            away_key = away.get("manager_key")
            if not home_key or not away_key:
                continue
            if home_key not in managers or away_key not in managers:
                continue

            home_score = safe_float(home.get("score"))
            away_score = safe_float(away.get("score"))
            year = safe_int(matchup.get("year"))

            managers[home_key]["playoff_seasons"].add(year)
            managers[away_key]["playoff_seasons"].add(year)

            if home_score > away_score:
                managers[home_key]["playoff_wins"] += 1
                managers[away_key]["playoff_losses"] += 1
            elif away_score > home_score:
                managers[away_key]["playoff_wins"] += 1
                managers[home_key]["playoff_losses"] += 1
            else:
                managers[home_key]["playoff_ties"] += 1
                managers[away_key]["playoff_ties"] += 1

    output = []
    for manager in managers.values():
        wins = manager["wins"]
        losses = manager["losses"]
        ties = manager["ties"]
        playoff_wins = manager["playoff_wins"]
        playoff_losses = manager["playoff_losses"]
        playoff_ties = manager["playoff_ties"]
        alias_rows = [
            {"name": name, "years": sorted(years)}
            for name, years in sorted(manager["aliases"].items(), key=lambda item: item[0].lower())
        ]
        output.append(
            {
                "manager_key": manager["manager_key"],
                "display": manager["display"],
                "default_display": manager["default_display"],
                "name_options": sorted(manager["name_options"]),
                "owner_ids": sorted(manager["owner_ids"]),
                "aliases": alias_rows,
                "team_refs": manager["team_refs"],
                "seasons": sorted(manager["seasons"]),
                "season_count": len(manager["seasons"]),
                "championships": manager["championships"],
                "top_three": manager["top_three"],
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "record": record_string(wins, losses, ties),
                "win_pct": pct(wins, losses, ties),
                "points_for": round1(manager["points_for"]),
                "points_against": round1(manager["points_against"]),
                "avg_points_for": round1(manager["points_for"] / max(1, len(manager["seasons"]))),
                "avg_rank": manager.get("avg_rank"),
                "acquisitions": manager["acquisitions"],
                "drops": manager["drops"],
                "trades": manager["trades"],
                "playoff_appearances": len(manager["playoff_seasons"]),
                "playoff_wins": playoff_wins,
                "playoff_losses": playoff_losses,
                "playoff_ties": playoff_ties,
                "playoff_record": record_string(playoff_wins, playoff_losses, playoff_ties),
                "playoff_win_pct": pct(playoff_wins, playoff_losses, playoff_ties),
            }
        )

    output.sort(key=lambda item: (-item["championships"], -item["wins"], -item["points_for"], item["display"]))
    return output


def assign_manager_colors(managers: list[dict[str, Any]]) -> None:
    for index, manager in enumerate(sorted(managers, key=lambda item: item["manager_key"])):
        manager["color"] = MANAGER_COLORS[index % len(MANAGER_COLORS)]


def manager_display_map(managers: list[dict[str, Any]]) -> dict[str, str]:
    return {manager["manager_key"]: manager["display"] for manager in managers}


def build_h2h(matchups: list[dict[str, Any]], managers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = manager_display_map(managers)
    rows: dict[tuple[str, str], dict[str, Any]] = {}

    for matchup in matchups:
        if matchup["stage"] != "regular" or not matchup["completed"]:
            continue
        home = matchup.get("home")
        away = matchup.get("away")
        if not home or not away or not home.get("manager_key") or not away.get("manager_key"):
            continue
        home_key = home["manager_key"]
        away_key = away["manager_key"]
        if home_key == away_key:
            continue
        pair = tuple(sorted((home_key, away_key)))
        row = rows.setdefault(
            pair,
            {
                "manager_a_key": pair[0],
                "manager_b_key": pair[1],
                "manager_a": names.get(pair[0], pair[0]),
                "manager_b": names.get(pair[1], pair[1]),
                "manager_a_wins": 0,
                "manager_b_wins": 0,
                "ties": 0,
                "games": 0,
                "points_a": 0.0,
                "points_b": 0.0,
            },
        )

        home_score = safe_float(home.get("score"))
        away_score = safe_float(away.get("score"))
        if home_key == pair[0]:
            row["points_a"] += home_score
            row["points_b"] += away_score
            a_score, b_score = home_score, away_score
        else:
            row["points_a"] += away_score
            row["points_b"] += home_score
            a_score, b_score = away_score, home_score

        row["games"] += 1
        if a_score > b_score:
            row["manager_a_wins"] += 1
        elif b_score > a_score:
            row["manager_b_wins"] += 1
        else:
            row["ties"] += 1

    output = []
    for row in rows.values():
        row["points_a"] = round1(row["points_a"])
        row["points_b"] = round1(row["points_b"])
        row["record"] = f"{row['manager_a_wins']}-{row['manager_b_wins']}"
        if row["ties"]:
            row["record"] += f"-{row['ties']}"
        output.append(row)

    output.sort(
        key=lambda item: (
            -item["games"],
            item["manager_a"].lower(),
            item["manager_b"].lower(),
        )
    )
    return output


def build_records(
    standings: list[dict[str, Any]],
    matchups: list[dict[str, Any]],
    managers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = manager_display_map(managers)
    records: list[dict[str, Any]] = []

    def add(category: str, label: str, value: str, detail: str, year: int | None = None) -> None:
        records.append(
            {
                "category": category,
                "label": label,
                "value": value,
                "detail": detail,
                "year": year,
            }
        )

    if standings:
        best_points = max(standings, key=lambda row: row["points_for"])
        worst_points = min((row for row in standings if row["points_for"] > 0), key=lambda row: row["points_for"])
        best_record = max(
            standings,
            key=lambda row: (row["win_pct"], row["wins"], row["points_for"]),
        )
        add(
            "Season",
            "Best season points",
            f"{best_points['points_for']:.1f}",
            f"{best_points['team_name']} ({names.get(best_points['manager_key'], best_points['owner_display'])})",
            best_points["year"],
        )
        add(
            "Season",
            "Lowest season points",
            f"{worst_points['points_for']:.1f}",
            f"{worst_points['team_name']} ({names.get(worst_points['manager_key'], worst_points['owner_display'])})",
            worst_points["year"],
        )
        add(
            "Season",
            "Best record",
            best_record["record"],
            f"{best_record['team_name']} ({best_record['win_pct']:.3f})",
            best_record["year"],
        )

    side_scores: list[dict[str, Any]] = []
    game_scores: list[dict[str, Any]] = []
    for matchup in matchups:
        home = matchup.get("home")
        away = matchup.get("away")
        for side_name, side in (("home", home), ("away", away)):
            if side and safe_float(side.get("score")) > 0:
                side_scores.append(
                    {
                        "year": matchup["year"],
                        "week": matchup["week"],
                        "stage": matchup["stage"],
                        "side": side_name,
                        "team_name": side["team_name"],
                        "manager_key": side.get("manager_key"),
                        "score": safe_float(side.get("score")),
                    }
                )
        if home and away and matchup.get("margin") is not None:
            game_scores.append(matchup)

    if side_scores:
        high = max(side_scores, key=lambda row: row["score"])
        low = min(side_scores, key=lambda row: row["score"])
        add(
            "Weekly",
            "Highest weekly score",
            f"{high['score']:.1f}",
            f"{high['team_name']} vs week {high['week']}",
            high["year"],
        )
        add(
            "Weekly",
            "Lowest weekly score",
            f"{low['score']:.1f}",
            f"{low['team_name']} vs week {low['week']}",
            low["year"],
        )

    if game_scores:
        largest = max(game_scores, key=lambda row: safe_float(row.get("margin")))
        closest = min(game_scores, key=lambda row: safe_float(row.get("margin")))

        def game_detail(matchup: dict[str, Any]) -> str:
            home = matchup["home"]
            away = matchup["away"]
            return (
                f"{away['team_name']} {away['score']:.1f} at "
                f"{home['team_name']} {home['score']:.1f}, week {matchup['week']}"
            )

        add(
            "Game",
            "Largest margin",
            f"{largest['margin']:.1f}",
            game_detail(largest),
            largest["year"],
        )
        add(
            "Game",
            "Closest game",
            f"{closest['margin']:.1f}",
            game_detail(closest),
            closest["year"],
        )

        losses = []
        for matchup in game_scores:
            home = matchup["home"]
            away = matchup["away"]
            if safe_float(home["score"]) > safe_float(away["score"]):
                losses.append({**away, "year": matchup["year"], "week": matchup["week"]})
            elif safe_float(away["score"]) > safe_float(home["score"]):
                losses.append({**home, "year": matchup["year"], "week": matchup["week"]})
        if losses:
            best_loss = max(losses, key=lambda row: safe_float(row["score"]))
            add(
                "Game",
                "Best losing score",
                f"{safe_float(best_loss['score']):.1f}",
                f"{best_loss['team_name']} in week {best_loss['week']}",
                best_loss["year"],
            )

    return records


def build_bundle(
    *,
    league_id: int,
    title: str,
    start_year: int,
    end_year: int,
    years: list[int],
    available_years: list[int],
    espn_s2: str,
    swid: str,
    manager_mappings: dict[str, Any],
    manual_overrides: dict[str, Any],
) -> dict[str, Any]:
    season_payloads = []
    for year in years:
        print(f"Fetching {year}...", flush=True)
        season_payloads.append(
            collect_season(
                league_id=league_id,
                year=year,
                espn_s2=espn_s2,
                swid=swid,
                manager_mappings=manager_mappings,
            )
        )

    seasons = [payload["season"] for payload in season_payloads]
    teams = [team for payload in season_payloads for team in payload["teams"]]
    standings = [row for payload in season_payloads for row in payload["standings"]]
    matchups = [row for payload in season_payloads for row in payload["matchups"]]
    applied_overrides = apply_manual_overrides(standings, matchups, manual_overrides)
    managers = summarize_managers(standings, teams, matchups)
    assign_manager_colors(managers)
    h2h = build_h2h(matchups, managers)
    records = build_records(standings, matchups, managers)

    return {
        "metadata": {
            "title": title,
            "league_id": league_id,
            "start_year": start_year,
            "end_year": end_year,
            "available_years": available_years,
            "selected_years": years,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "ESPN Fantasy Football API via vendored espn-api",
            "manual_overrides": applied_overrides,
        },
        "seasons": seasons,
        "teams": teams,
        "standings": standings,
        "matchups": matchups,
        "managers": managers,
        "h2h": h2h,
        "records": records,
    }


def json_for_script(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")


def render_html(data: dict[str, Any]) -> str:
    payload = json_for_script(data)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The History of True</title>
  <style>
    :root {{
      --bg: #f7f5ef;
      --panel: #ffffff;
      --ink: #18202a;
      --muted: #667085;
      --line: #d9dee5;
      --blue: #2f5d7c;
      --green: #3f7d58;
      --red: #b94b5f;
      --gold: #c58b22;
      --shadow: 0 12px 30px rgba(24, 32, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      line-height: 1.45;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: #fbfaf7;
    }}
    .wrap {{
      width: min(1360px, calc(100vw - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: end;
      padding: 24px 0 18px;
    }}
    h1 {{
      margin: 0;
      font-size: 32px;
      line-height: 1.1;
      letter-spacing: 0;
    }}
    .meta {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 14px;
    }}
    .tabs {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      padding: 0 0 14px;
    }}
    .tab {{
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 8px;
      min-height: 36px;
      padding: 0 13px;
      cursor: pointer;
      font-weight: 650;
    }}
    .tab.active {{
      background: var(--blue);
      border-color: var(--blue);
      color: #fff;
    }}
    main {{ padding: 20px 0 36px; }}
    .view {{ display: none; }}
    .view.active {{ display: block; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
      min-width: 0;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }}
    .card:hover {{
      transform: translateY(-2px);
      box-shadow: 0 20px 40px rgba(24, 32, 42, 0.12);
    }}
    .card h2, .panel h2 {{
      margin: 0 0 10px;
      font-size: 17px;
      line-height: 1.2;
      letter-spacing: 0;
    }}
    .stat-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      font-weight: 750;
    }}
    .stat-value {{
      margin-top: 5px;
      font-size: 30px;
      line-height: 1.1;
      font-weight: 760;
    }}
    .stat-note {{
      margin-top: 5px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .columns {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 16px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 16px;
      box-shadow: var(--shadow);
      min-width: 0;
    }}
    .controls {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
      margin: 0 0 12px;
    }}
    label {{
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    select, input[type="search"] {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--ink);
      padding: 0 10px;
      font: inherit;
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
      background: #fff;
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    tr:nth-child(even) {{
      background: #f8f9fa;
    }}
    tr:hover {{
      background: #f0f4ff;
    }}
    th {{
      position: sticky;
      top: 0;
      background: #f1f4f6;
      color: #344054;
      font-size: 12px;
      text-transform: uppercase;
      cursor: pointer;
      user-select: none;
      z-index: 1;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .muted {{ color: var(--muted); }}
    .h2h-matrix-wrap {{
      overflow: auto;
      max-height: 68vh;
    }}
    .h2h-matrix {{
      width: max-content;
      min-width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      background: #fff;
    }}
    .h2h-matrix th,
    .h2h-matrix td {{
      padding: 6px 8px;
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      font-size: 12px;
      line-height: 1.25;
      white-space: nowrap;
      vertical-align: middle;
      text-align: center;
      font-variant-numeric: tabular-nums;
    }}
    .h2h-matrix thead th {{
      position: sticky;
      top: 0;
      z-index: 3;
      background: #f1f4f6;
      text-transform: none;
      font-size: 11px;
      font-weight: 760;
      cursor: default;
    }}
    .h2h-matrix .h2h-corner {{
      left: 0;
      z-index: 5;
      min-width: 190px;
      text-align: left;
    }}
    .h2h-matrix .h2h-row-head {{
      position: sticky;
      left: 0;
      z-index: 4;
      background: #f8fafc;
      text-align: left;
      font-weight: 700;
      min-width: 190px;
      max-width: 220px;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .h2h-matrix .h2h-cell {{
      min-width: 62px;
      max-width: 68px;
    }}
    .h2h-matrix .h2h-diag {{
      background: #f9fafb;
      color: #98a2b3;
      font-weight: 700;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 7px;
      padding: 0 7px;
      background: #edf3f0;
      color: var(--green);
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }}
    .stage-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 7px;
      padding: 0 7px;
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }}
    .stage-regular {{
      background: #edf3f0;
      color: var(--green);
    }}
    .stage-playoff {{
      background: #fff4d8;
      color: #8a5a00;
      border: 1px solid #f0d38a;
      animation: pulse 2s infinite;
    }}
    .stage-consolation {{
      background: #edf5ff;
      color: #1f4a7a;
      border: 1px solid #bfd8f7;
    }}
    .stage-postseason {{
      background: #f3f4f6;
      color: #374151;
      border: 1px solid #d5d8de;
    }}
    @keyframes pulse {{
      0%, 100% {{ opacity: 1; }}
      50% {{ opacity: 0.8; }}
    }}
    tr.playoff-row td {{
      background: #fffaf0;
    }}
    tr.playoff-row td:first-child {{
      box-shadow: inset 4px 0 0 var(--gold);
    }}
    tr.consolation-row td {{
      background: #f7fbff;
    }}
    tr.consolation-row td:first-child {{
      box-shadow: inset 4px 0 0 #4f79a6;
    }}
    .manager-label, .team-label {{
      display: inline-flex;
      min-width: 0;
    }}
    .team-label {{
      flex-direction: column;
      align-items: flex-start;
      gap: 4px;
    }}
    .team-label-main {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
    }}
    .manager-bubble, .year-bubble, .metric-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border: 1px solid transparent;
      border-radius: 999px;
      padding: 0 9px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .year-bubble {{
      min-height: 20px;
      font-size: 11px;
      letter-spacing: 0.02em;
      padding: 0 8px;
    }}
    .metric-badge {{
      border-radius: 8px;
      background: #edf1f6;
      border-color: #dce2e9;
      color: #1f2a37;
    }}
    .metric-good {{
      background: #e9f8ef;
      border-color: #b8e6c7;
      color: #14532d;
    }}
    .metric-mid {{
      background: #eef2f7;
      border-color: #d5dde6;
      color: #334155;
    }}
    .metric-bad {{
      background: #fff0f0;
      border-color: #f7c4c4;
      color: #9f1239;
    }}
    tr.champion-row td {{
      background: #fffaf0;
    }}
    tr.champion-row td:first-child {{
      box-shadow: inset 4px 0 0 var(--gold);
    }}
    .leader-table table {{
      min-width: 680px;
    }}
    .bars {{ display: grid; gap: 9px; }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(130px, 190px) minmax(120px, 1fr) 72px;
      gap: 10px;
      align-items: center;
      min-height: 26px;
      font-size: 14px;
    }}
    .bar-track {{
      height: 12px;
      background: #edf0f3;
      border-radius: 8px;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      width: 0%;
      background: var(--blue);
      border-radius: 8px;
    }}
    .bar-fill.green {{ background: var(--green); }}
    .bar-fill.gold {{ background: var(--gold); }}
    .bar-fill.red {{ background: var(--red); }}
    .alias-list {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      max-width: 520px;
    }}
    .alias {{
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 3px 7px;
      background: #fbfaf7;
      font-size: 12px;
      color: #344054;
    }}
    .alias-current {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 3px 8px;
      background: #fbfaf7;
      color: #344054;
      max-width: 320px;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .alias-hover {{
      position: relative;
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      cursor: help;
    }}
    .alias-tooltip {{
      position: absolute;
      left: 0;
      top: calc(100% + 7px);
      min-width: 220px;
      max-width: 420px;
      padding: 8px 10px;
      border-radius: 8px;
      border: 1px solid #ccd5df;
      background: #ffffff;
      box-shadow: 0 10px 24px rgba(24, 32, 42, 0.18);
      color: #1f2937;
      font-size: 12px;
      line-height: 1.35;
      white-space: normal;
      opacity: 0;
      visibility: hidden;
      transform: translateY(-2px);
      transition: opacity 0.15s ease, transform 0.15s ease;
      z-index: 20;
    }}
    .alias-hover:hover .alias-tooltip,
    .alias-hover:focus-within .alias-tooltip {{
      opacity: 1;
      visibility: visible;
      transform: translateY(0);
    }}
    .titles-emoji {{
      font-size: 16px;
      letter-spacing: 0.04em;
      white-space: nowrap;
    }}
    .matchup-board {{
      display: grid;
      gap: 12px;
    }}
    .matchup-year {{
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      padding: 10px;
    }}
    .matchup-year-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }}
    .matchup-year-count {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .matchup-round {{
      margin-top: 8px;
    }}
    .matchup-round-title {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 6px;
      color: #344054;
      font-size: 12px;
      font-weight: 760;
      text-transform: uppercase;
      letter-spacing: 0.02em;
    }}
    .matchup-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
      gap: 8px;
    }}
    .matchup-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fcfcfd;
      padding: 8px;
      min-width: 0;
    }}
    .matchup-card.playoff {{
      border-color: #f0d38a;
      background: #fffaf0;
    }}
    .matchup-card.consolation {{
      border-color: #bfd8f7;
      background: #f7fbff;
    }}
    .matchup-card.postseason {{
      border-color: #d5d8de;
      background: #f9fafb;
    }}
    .matchup-card-head {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 7px;
    }}
    .week-chip {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid #d5dde6;
      background: #eef2f7;
      color: #334155;
      font-size: 11px;
      font-weight: 760;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .round-chip {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid #f0d38a;
      background: #fff4d8;
      color: #8a5a00;
      font-size: 11px;
      font-weight: 760;
      white-space: nowrap;
    }}
    .matchup-team-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 8px;
      border: 1px solid #e4e8ef;
      border-radius: 7px;
      padding: 4px 6px;
      margin-bottom: 5px;
      background: #f8fafc;
      min-width: 0;
    }}
    .matchup-team-row:last-of-type {{
      margin-bottom: 0;
    }}
    .matchup-team-row.winner {{
      background: #e9f8ef;
      border-color: #b8e6c7;
    }}
    .matchup-team-row.loser {{
      opacity: 0.92;
    }}
    .matchup-team-meta {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 6px;
      min-width: 0;
    }}
    .matchup-team-name {{
      font-size: 13px;
      font-weight: 700;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .matchup-team-row .manager-bubble {{
      min-height: 18px;
      padding: 0 7px;
      font-size: 11px;
    }}
    .score-chip {{
      display: inline-flex;
      align-items: center;
      min-height: 20px;
      border-radius: 7px;
      padding: 0 7px;
      border: 1px solid #d5dde6;
      background: #eef2f7;
      color: #334155;
      font-size: 12px;
      font-weight: 760;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
    }}
    .score-chip.win {{
      background: #e9f8ef;
      border-color: #b8e6c7;
      color: #14532d;
    }}
    .score-chip.loss {{
      background: #fff0f0;
      border-color: #f7c4c4;
      color: #9f1239;
    }}
    .score-chip.tie {{
      background: #eef2f7;
      border-color: #d5dde6;
      color: #334155;
    }}
    .matchup-footer {{
      margin-top: 6px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
    }}
    .matchup-empty {{
      color: var(--muted);
      font-size: 13px;
      padding: 8px 2px;
    }}
    @media (max-width: 940px) {{
      .topbar, .columns {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}
    @media (max-width: 620px) {{
      .wrap {{ width: min(100vw - 20px, 1360px); }}
      h1 {{ font-size: 25px; }}
      .grid {{ grid-template-columns: 1fr; }}
      .bar-row {{ grid-template-columns: 1fr; gap: 4px; }}
      table {{ min-width: 680px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="topbar">
        <div>
          <h1 id="pageTitle"></h1>
          <div class="meta" id="pageMeta"></div>
        </div>
        <div class="meta" id="generatedAt"></div>
      </div>
      <nav class="tabs" aria-label="Dashboard sections">
        <button class="tab active" data-tab="overview">Overview</button>
        <button class="tab" data-tab="seasons">Seasons</button>
        <button class="tab" data-tab="managers">Managers</button>
        <button class="tab" data-tab="matchups">Matchups</button>
        <button class="tab" data-tab="records">Records</button>
      </nav>
    </div>
  </header>
  <main class="wrap">
    <section class="view active" id="overview"></section>
    <section class="view" id="seasons"></section>
    <section class="view" id="managers"></section>
    <section class="view" id="matchups"></section>
    <section class="view" id="records"></section>
  </main>
  <script>
    const DATA = {payload};

    const byId = (id) => document.getElementById(id);
    const fmt = (value, digits = 1) => Number(value || 0).toFixed(digits);
    const intFmt = (value) => Number(value || 0).toLocaleString();

    const managerMap = new Map(DATA.managers.map((manager) => [manager.manager_key, manager]));
    const YEAR_COLORS = ["#2f5d7c", "#3f7d58", "#9a5b13", "#9f3a4f", "#5a4f9f", "#24706f", "#7b4b2a", "#6a6f2b"];
    const ALL_YEARS = [...new Set((DATA.metadata.selected_years || DATA.metadata.available_years || DATA.seasons.map((season) => season.year)).map((year) => Number(year)).filter((year) => Number.isFinite(year)))].sort((a, b) => a - b);
    const TEN_TEAM_YEARS = DATA.seasons
      .filter((season) => Number(season.team_count || 0) === 10)
      .map((season) => Number(season.year))
      .filter((year) => Number.isFinite(year))
      .sort((a, b) => a - b);
    const TEN_TEAM_RANGE = TEN_TEAM_YEARS.length ? [TEN_TEAM_YEARS[0], TEN_TEAM_YEARS[TEN_TEAM_YEARS.length - 1]] : null;
    const yearColorMap = new Map(
      [...DATA.seasons]
        .sort((a, b) => a.year - b.year)
        .map((season, index) => [String(season.year), YEAR_COLORS[index % YEAR_COLORS.length]])
    );

    function managerName(key) {{
      return managerMap.get(key)?.display || key || "Unknown Owner";
    }}

    function managerAvgPpg(manager) {{
      const games = Number(manager.wins || 0) + Number(manager.losses || 0) + Number(manager.ties || 0);
      return games ? Number(manager.points_for || 0) / games : 0;
    }}

    function inferBracketTypesFromSeeds() {{
      const standingsByYear = new Map();
      DATA.standings.forEach((row) => {{
        const year = Number(row.year || 0);
        if (!standingsByYear.has(year)) standingsByYear.set(year, []);
        standingsByYear.get(year).push(row);
      }});

      DATA.seasons.forEach((season) => {{
        const year = Number(season.year || 0);
        const playoffTeamCount = Number(season.playoff_team_count || 0);
        if (playoffTeamCount <= 0) return;

        const yearStandings = standingsByYear.get(year) || [];
        let contenders = new Set(
          yearStandings
            .filter((row) => Number(row.playoff_seed || 0) > 0 && Number(row.playoff_seed || 0) <= playoffTeamCount)
            .map((row) => Number(row.team_id || 0))
            .filter((teamId) => teamId > 0)
        );

        if (contenders.size < 2) {{
          contenders = new Set(
            [...yearStandings]
              .filter((row) => Number(row.team_id || 0) > 0 && Number(row.final_rank || 0) > 0)
              .sort((a, b) => Number(a.final_rank || 999) - Number(b.final_rank || 999))
              .slice(0, playoffTeamCount)
              .map((row) => Number(row.team_id || 0))
          );
        }}

        if (contenders.size < 2) return;

        const yearPlayoffs = DATA.matchups
          .filter((matchup) => Number(matchup.year || 0) === year && matchup.stage === "playoff" && matchup.home && matchup.away)
          .sort((a, b) => Number(a.week || 0) - Number(b.week || 0));
        const weeks = [...new Set(yearPlayoffs.map((matchup) => Number(matchup.week || 0)).filter((week) => week > 0))];

        weeks.forEach((week) => {{
          const weekGames = yearPlayoffs.filter((matchup) => Number(matchup.week || 0) === week);
          const nextContenders = new Set();
          const playedContenders = new Set();

          weekGames.forEach((matchup) => {{
            const homeId = Number((matchup.home || {{}}).team_id || 0);
            const awayId = Number((matchup.away || {{}}).team_id || 0);
            const homeContender = contenders.has(homeId);
            const awayContender = contenders.has(awayId);
            if (homeContender) playedContenders.add(homeId);
            if (awayContender) playedContenders.add(awayId);

            const inferred = homeContender && awayContender ? "playoff" : "consolation";
            const winner = String(matchup.winner || "").toUpperCase();
            if (homeContender && awayContender) {{
              if (winner === "HOME") nextContenders.add(homeId);
              else if (winner === "AWAY") nextContenders.add(awayId);
              else {{
                nextContenders.add(homeId);
                nextContenders.add(awayId);
              }}
            }}

            const tier = String(matchup.playoff_tier || "NONE").toUpperCase();
            if ((!matchup.bracket_type || matchup.bracket_type === "postseason") && tier === "NONE") {{
              matchup.bracket_type = inferred;
              matchup.bracket_source = "inferred_client";
            }}
          }});

          const byes = [...contenders].filter((teamId) => !playedContenders.has(teamId));
          contenders = new Set([...nextContenders, ...byes]);
        }});
      }});
    }}

    function numericExtent(values) {{
      const nums = (values || []).map((value) => Number(value)).filter((value) => Number.isFinite(value));
      if (!nums.length) return [0, 0];
      return [Math.min(...nums), Math.max(...nums)];
    }}

    function hexToRgba(hex, alpha) {{
      const raw = String(hex || "").trim().replace("#", "");
      if (raw.length !== 6) return "rgba(102, 112, 133, " + alpha + ")";
      const intValue = Number.parseInt(raw, 16);
      const r = (intValue >> 16) & 255;
      const g = (intValue >> 8) & 255;
      const b = intValue & 255;
      return "rgba(" + r + ", " + g + ", " + b + ", " + alpha + ")";
    }}

    function managerColor(managerKey) {{
      return managerMap.get(managerKey)?.color || "#667085";
    }}

    function yearColor(year) {{
      return yearColorMap.get(String(year)) || "#475467";
    }}

    function bubble(text, color, className) {{
      const node = el("span", className, text);
      node.style.background = hexToRgba(color, 0.16);
      node.style.borderColor = hexToRgba(color, 0.4);
      node.style.color = color;
      return node;
    }}

    function metricBadge(value, tone = "mid") {{
      return el("span", "metric-badge metric-" + tone, value);
    }}

    function heatBadge(value, minValue, maxValue, digits = 1, invert = false) {{
      const num = Number(value || 0);
      const low = Number.isFinite(Number(minValue)) ? Number(minValue) : num;
      const high = Number.isFinite(Number(maxValue)) ? Number(maxValue) : num;
      const rawRatio = high > low ? (num - low) / (high - low) : 0.5;
      const ratio = Math.max(0, Math.min(1, invert ? 1 - rawRatio : rawRatio));
      const hue = Math.round(12 + (ratio * 115));
      const node = el("span", "metric-badge");
      node.textContent = digits == null ? String(num) : Number(num).toFixed(digits);
      node.style.background = "hsla(" + hue + ", 72%, 44%, 0.16)";
      node.style.borderColor = "hsla(" + hue + ", 72%, 35%, 0.38)";
      node.style.color = "hsl(" + hue + ", 78%, 24%)";
      return node;
    }}

    function managerBubble(managerKey) {{
      return bubble(managerName(managerKey), managerColor(managerKey), "manager-bubble");
    }}

    function yearBubble(year) {{
      if (year == null || year === "") return el("span", "muted", "");
      return bubble(String(year), yearColor(year), "year-bubble");
    }}

    function winPctBadge(winPct) {{
      const value = Number(winPct || 0);
      const tone = value >= 0.62 ? "good" : value >= 0.5 ? "mid" : "bad";
      return metricBadge(value.toFixed(3), tone);
    }}

    function rankBadge(rank) {{
      const value = Number(rank || 0);
      if (!Number.isFinite(value) || value <= 0) return metricBadge("-", "mid");
      const tone = value <= 3 ? "good" : value <= 6 ? "mid" : "bad";
      return metricBadge(String(value), tone);
    }}

    function titleBadge(isChampion) {{
      return isChampion ? metricBadge("🏆 Champion", "good") : el("span", "muted", "");
    }}

    function scoreBadge(score, opponentScore) {{
      const value = Number(score || 0);
      const other = Number(opponentScore || 0);
      const tone = value > other ? "good" : value < other ? "bad" : "mid";
      return metricBadge(value.toFixed(1), tone);
    }}

    function marginBadge(margin) {{
      const value = Number(margin || 0);
      const tone = value <= 4 ? "good" : value <= 12 ? "mid" : "bad";
      return metricBadge(value.toFixed(1), tone);
    }}

    function managerLabel(managerKey) {{
      const node = el("span", "manager-label");
      node.append(managerBubble(managerKey));
      return node;
    }}

    function teamLabel(side) {{
      const node = el("span", "team-label");
      node.append(el("span", "team-label-main", side?.team_name || "Unknown"));
      node.append(managerBubble(side?.manager_key));
      return node;
    }}

    function matchupBucket(matchup) {{
      if (!matchup) return "regular";
      if (matchup.bracket_type) return matchup.bracket_type;
      if (matchup.stage !== "playoff") return "regular";
      const tier = String(matchup.playoff_tier || "NONE").toUpperCase();
      if (tier === "NONE") return "postseason";
      if (tier.includes("CONSOLATION") || tier.includes("LADDER") || tier.startsWith("LOSERS_")) return "consolation";
      if (tier.includes("WINNERS_BRACKET")) return "playoff";
      return "postseason";
    }}

    function stageBadge(matchup) {{
      const bucket = matchupBucket(matchup);
      if (bucket === "playoff") return el("span", "stage-badge stage-playoff", "Playoff Bracket");
      if (bucket === "consolation") return el("span", "stage-badge stage-consolation", "Consolation Ladder");
      if (bucket === "postseason") return el("span", "stage-badge stage-postseason", "Postseason");
      return el("span", "stage-badge stage-regular", "Regular");
    }}

    function matchupOutcome(matchup) {{
      const homeScore = Number(matchup?.home?.score || 0);
      const awayScore = Number(matchup?.away?.score || 0);
      if (homeScore > awayScore) return {{ home: "win", away: "loss", margin: homeScore - awayScore }};
      if (awayScore > homeScore) return {{ home: "loss", away: "win", margin: awayScore - homeScore }};
      return {{ home: "tie", away: "tie", margin: 0 }};
    }}

    function playoffRoundLabel(year, week) {{
      const playoffWeeks = [...new Set(
        DATA.matchups
          .filter((row) => Number(row.year || 0) === Number(year) && row.completed && row.home && row.away && matchupBucket(row) === "playoff")
          .map((row) => Number(row.week || 0))
          .filter((value) => value > 0)
      )].sort((a, b) => a - b);
      const idx = playoffWeeks.indexOf(Number(week || 0));
      const total = playoffWeeks.length;
      if (idx < 0 || total === 0) return "Playoff Round";
      if (total === 1) return "Championship";
      if (total === 2) return idx === 0 ? "Semifinals" : "Championship";
      if (total === 3) return idx === 0 ? "Quarterfinals" : idx === 1 ? "Semifinals" : "Championship";
      if (idx === total - 1) return "Championship";
      if (idx === total - 2) return "Semifinals";
      if (idx === total - 3) return "Quarterfinals";
      return "Round " + (idx + 1);
    }}

    function matchupCard(matchup, options = {{}}) {{
      const roundLabel = options.roundLabel || "";
      const showWeekChip = options.showWeekChip !== false;
      const showStageChip = options.showStageChip === true;
      const showRoundChip = options.showRoundChip === true;
      const bucket = matchupBucket(matchup);
      const card = el("article", "matchup-card " + bucket);
      const head = el("div", "matchup-card-head");
      if (showWeekChip) {{
        head.append(el("span", "week-chip", "Week " + matchup.week));
      }}
      if (showStageChip) {{
        head.append(stageBadge(matchup));
      }}
      if (bucket === "playoff" && roundLabel && showRoundChip) {{
        head.append(el("span", "round-chip", roundLabel));
      }}
      card.append(head);

      const outcome = matchupOutcome(matchup);
      const rows = [
        {{ side: matchup.away, result: outcome.away }},
        {{ side: matchup.home, result: outcome.home }},
      ];
      rows.forEach((row) => {{
        const teamRow = el("div", "matchup-team-row " + (row.result === "win" ? "winner" : row.result === "loss" ? "loser" : ""));
        const meta = el("div", "matchup-team-meta");
        meta.append(el("span", "matchup-team-name", row.side?.team_name || "Unknown"));
        meta.append(managerBubble(row.side?.manager_key));
        teamRow.append(meta);
        teamRow.append(el("span", "score-chip " + row.result, Number(row.side?.score || 0).toFixed(1)));
        card.append(teamRow);
      }});

      const footer = el("div", "matchup-footer");
      footer.append(yearBubble(matchup.year));
      footer.append(el("span", "", "Margin " + Number(matchup.margin || outcome.margin || 0).toFixed(1)));
      card.append(footer);
      return card;
    }}

    function setText(node, value) {{
      node.textContent = value == null ? "" : String(value);
      return node;
    }}

    function el(tag, className, text) {{
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) setText(node, text);
      return node;
    }}

    function card(label, value, note) {{
      const node = el("div", "card");
      node.append(el("div", "stat-label", label));
      node.append(el("div", "stat-value", value));
      node.append(el("div", "stat-note", note));
      return node;
    }}

    function table(headers, rows) {{
      const wrap = el("div", "table-wrap");
      const tbl = document.createElement("table");
      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      headers.forEach((header, idx) => {{
        const th = el("th", header.num ? "num" : "", header.label);
        th.addEventListener("click", () => sortTable(tbl, idx, header.num));
        headRow.append(th);
      }});
      thead.append(headRow);
      const tbody = document.createElement("tbody");
      rows.forEach((row) => {{
        const cells = Array.isArray(row) ? row : row.cells;
        const tr = document.createElement("tr");
        if (!Array.isArray(row) && row.className) tr.className = row.className;
        cells.forEach((cell, idx) => {{
          const td = el("td", headers[idx]?.num ? "num" : "");
          let content = cell;
          let sortValue = null;
          if (
            cell &&
            typeof cell === "object" &&
            !(cell instanceof Node) &&
            Object.prototype.hasOwnProperty.call(cell, "content")
          ) {{
            content = cell.content;
            sortValue = cell.sortValue;
          }}
          if (sortValue !== null && sortValue !== undefined) {{
            td.dataset.sortValue = String(sortValue);
          }}
          if (content instanceof Node) td.append(content);
          else setText(td, content);
          tr.append(td);
        }});
        tbody.append(tr);
      }});
      tbl.append(thead, tbody);
      wrap.append(tbl);
      return wrap;
    }}

    function sortTable(tbl, col, numeric) {{
      const tbody = tbl.querySelector("tbody");
      const rows = Array.from(tbody.querySelectorAll("tr"));
      const current = tbl.dataset.sortCol === String(col) && tbl.dataset.sortDir === "asc" ? "desc" : "asc";
      tbl.dataset.sortCol = String(col);
      tbl.dataset.sortDir = current;
      rows.sort((a, b) => {{
        const av = a.children[col]?.dataset.sortValue ?? a.children[col]?.textContent.trim() ?? "";
        const bv = b.children[col]?.dataset.sortValue ?? b.children[col]?.textContent.trim() ?? "";
        const cmp = numeric ? Number(av.replace(/[^0-9.-]/g, "")) - Number(bv.replace(/[^0-9.-]/g, "")) : av.localeCompare(bv);
        return current === "asc" ? cmp : -cmp;
      }});
      tbody.replaceChildren(...rows);
    }}

    function options(select, values, selected) {{
      select.replaceChildren();
      values.forEach(([value, label]) => {{
        const opt = document.createElement("option");
        opt.value = value;
        opt.textContent = label;
        if (String(value) === String(selected)) opt.selected = true;
        select.append(opt);
      }});
    }}

    function defaultRangeState() {{
      if (TEN_TEAM_RANGE) {{
        return {{ preset: "ten_team", startYear: TEN_TEAM_RANGE[0], endYear: TEN_TEAM_RANGE[1] }};
      }}
      if (ALL_YEARS.length) {{
        return {{ preset: "all", startYear: ALL_YEARS[0], endYear: ALL_YEARS[ALL_YEARS.length - 1] }};
      }}
      return {{
        preset: "all",
        startYear: Number(DATA.metadata.start_year || 0),
        endYear: Number(DATA.metadata.end_year || 0)
      }};
    }}

    function applyRangePreset(state, preset) {{
      if (preset === "ten_team" && TEN_TEAM_RANGE) {{
        return {{ preset: "ten_team", startYear: TEN_TEAM_RANGE[0], endYear: TEN_TEAM_RANGE[1] }};
      }}
      if (preset === "all") {{
        return {{
          preset: "all",
          startYear: ALL_YEARS[0],
          endYear: ALL_YEARS[ALL_YEARS.length - 1]
        }};
      }}
      return {{ ...state, preset: "custom" }};
    }}

    function normalizeRangeState(state) {{
      const defaults = defaultRangeState();
      const minYear = ALL_YEARS.length ? ALL_YEARS[0] : defaults.startYear;
      const maxYear = ALL_YEARS.length ? ALL_YEARS[ALL_YEARS.length - 1] : defaults.endYear;
      let startYear = Number.isFinite(Number(state.startYear)) ? Number(state.startYear) : defaults.startYear;
      let endYear = Number.isFinite(Number(state.endYear)) ? Number(state.endYear) : defaults.endYear;

      startYear = Math.min(Math.max(startYear, minYear), maxYear);
      endYear = Math.min(Math.max(endYear, minYear), maxYear);
      if (startYear > endYear) {{
        [startYear, endYear] = [endYear, startYear];
      }}

      return {{
        preset: state.preset || defaults.preset,
        startYear,
        endYear,
      }};
    }}

    function readRangeState(root) {{
      const defaults = defaultRangeState();
      const state = normalizeRangeState({{
        preset: root.dataset.rangePreset || defaults.preset,
        startYear: root.dataset.rangeStart || defaults.startYear,
        endYear: root.dataset.rangeEnd || defaults.endYear,
      }});
      root.dataset.rangePreset = state.preset;
      root.dataset.rangeStart = String(state.startYear);
      root.dataset.rangeEnd = String(state.endYear);
      return state;
    }}

    function writeRangeState(root, state) {{
      const normalized = normalizeRangeState(state);
      root.dataset.rangePreset = normalized.preset;
      root.dataset.rangeStart = String(normalized.startYear);
      root.dataset.rangeEnd = String(normalized.endYear);
      return normalized;
    }}

    function inYearRange(year, state) {{
      const value = Number(year || 0);
      return value >= state.startYear && value <= state.endYear;
    }}

    function filterByRange(rows, state, yearSelector = (row) => row.year) {{
      return rows.filter((row) => inYearRange(yearSelector(row), state));
    }}

    function attachRangeControls(controls, root, rerender) {{
      if (!ALL_YEARS.length) return readRangeState(root);
      let state = readRangeState(root);

      controls.append(el("label", "", "Range"));
      const presetOptions = [["all", "All years"]];
      if (TEN_TEAM_RANGE) {{
        presetOptions.unshift(["ten_team", "10-team era"]);
      }}
      presetOptions.push(["custom", "Custom"]);

      const presetSelect = document.createElement("select");
      options(presetSelect, presetOptions, state.preset);
      presetSelect.addEventListener("change", () => {{
        state = applyRangePreset(state, presetSelect.value);
        state = writeRangeState(root, state);
        rerender();
      }});
      controls.append(presetSelect);

      controls.append(el("label", "", "From"));
      const startSelect = document.createElement("select");
      options(startSelect, ALL_YEARS.map((year) => [year, String(year)]), state.startYear);
      startSelect.addEventListener("change", () => {{
        const nextStart = Number(startSelect.value);
        const nextEnd = Math.max(nextStart, Number(root.dataset.rangeEnd || nextStart));
        state = writeRangeState(root, {{ preset: "custom", startYear: nextStart, endYear: nextEnd }});
        rerender();
      }});
      controls.append(startSelect);

      controls.append(el("label", "", "To"));
      const endSelect = document.createElement("select");
      options(endSelect, ALL_YEARS.map((year) => [year, String(year)]), state.endYear);
      endSelect.addEventListener("change", () => {{
        const nextEnd = Number(endSelect.value);
        const nextStart = Math.min(Number(root.dataset.rangeStart || nextEnd), nextEnd);
        state = writeRangeState(root, {{ preset: "custom", startYear: nextStart, endYear: nextEnd }});
        rerender();
      }});
      controls.append(endSelect);

      return state;
    }}

    function barChart(rows, color = "blue") {{
      const node = el("div", "bars");
      const max = Math.max(1, ...rows.map((row) => Number(row.value) || 0));
      rows.forEach((row) => {{
        const item = el("div", "bar-row");
        const label = el("div", "");
        if (row.label instanceof Node) label.append(row.label);
        else setText(label, row.label);
        item.append(label);
        const track = el("div", "bar-track");
        const fill = el("div", "bar-fill " + color);
        fill.style.width = Math.max(2, (Number(row.value) / max) * 100) + "%";
        track.append(fill);
        item.append(track);
        item.append(el("div", "num", row.display || fmt(row.value)));
        node.append(item);
      }});
      return node;
    }}

    function aliasList(aliases) {{
      const node = el("div", "alias-list");
      aliases.forEach((alias) => {{
        node.append(el("span", "alias", alias.name + " (" + alias.years.join(", ") + ")"));
      }});
      return node;
    }}

    function regularMatchups() {{
      return DATA.matchups.filter((matchup) => matchup.stage === "regular" && matchup.completed && matchup.home && matchup.away);
    }}

    function recordCell(wins, losses, ties) {{
      return ties ? wins + "-" + losses + "-" + ties : wins + "-" + losses;
    }}

    function pctValue(wins, losses, ties) {{
      const games = wins + losses + ties;
      if (!games) return 0;
      return (wins + ties * 0.5) / games;
    }}

    function yearsTooltip(years) {{
      const values = (years || []).map((year) => Number(year)).filter((year) => Number.isFinite(year)).sort((a, b) => a - b);
      if (!values.length) return "";
      return values.join(", ");
    }}

    function titlesEmojiCell(count, years = []) {{
      const value = Number(count || 0);
      if (value <= 0) return el("span", "muted", "");
      const node = el("span", "titles-emoji", "🏆".repeat(value));
      node.title = yearsTooltip(years);
      return node;
    }}

    function postseasonAppearancesCell(count, years = []) {{
      const value = Number(count || 0);
      if (value <= 0) return el("span", "muted", "0");
      const tone = value >= 5 ? "good" : value >= 2 ? "mid" : "bad";
      const node = metricBadge(String(value), tone);
      node.title = yearsTooltip(years);
      return node;
    }}

    function aliasHoverCell(currentAlias, otherAliases) {{
      const current = currentAlias || "-";
      const others = otherAliases || [];
      if (!others.length) return el("span", "alias-current", current);

      const wrapper = el("span", "alias-hover");
      wrapper.append(el("span", "alias-current", current));
      const formatted = others.map((alias) => alias.name + " (" + alias.years.join(", ") + ")");
      wrapper.title = "Other aliases: " + formatted.join(" • ");
      wrapper.tabIndex = 0;

      const tooltip = el("span", "alias-tooltip");
      tooltip.textContent = formatted.join(" • ");
      wrapper.append(tooltip);
      return wrapper;
    }}

    function playoffRecordCell(manager) {{
      const wins = Number(manager.playoff_wins || 0);
      const losses = Number(manager.playoff_losses || 0);
      const ties = Number(manager.playoff_ties || 0);
      const games = wins + losses + ties;
      if (!games) return el("span", "muted", "-");
      const winPct = pctValue(wins, losses, ties);
      const tone = winPct >= 0.6 ? "good" : winPct >= 0.5 ? "mid" : "bad";
      return metricBadge(recordCell(wins, losses, ties), tone);
    }}

    function managersForRange(state) {{
      const teamRows = filterByRange(DATA.teams, state, (row) => row.year);
      const standingRows = filterByRange(DATA.standings, state, (row) => row.year);
      const playoffMatchups = filterByRange(DATA.matchups, state, (row) => row.year)
        .filter((matchup) => matchup.completed && matchup.home && matchup.away && matchupBucket(matchup) === "playoff");
      const managers = new Map();
      const latestYear = new Map();

      function ensureManager(key) {{
        if (!managers.has(key)) {{
          managers.set(key, {{
            manager_key: key,
            display: managerName(key),
            aliases: new Map(),
            seasons: new Set(),
            championships: 0,
            title_years: new Set(),
            wins: 0,
            losses: 0,
            ties: 0,
            points_for: 0,
            points_against: 0,
            ranks: [],
            playoff_wins: 0,
            playoff_losses: 0,
            playoff_ties: 0,
            playoff_appearance_years: new Set(),
          }});
        }}
        return managers.get(key);
      }}

      teamRows.forEach((team) => {{
        if (!team.manager_key) return;
        const manager = ensureManager(team.manager_key);
        manager.seasons.add(Number(team.year || 0));
        if (!manager.aliases.has(team.team_name)) manager.aliases.set(team.team_name, new Set());
        manager.aliases.get(team.team_name).add(Number(team.year || 0));

        const year = Number(team.year || 0);
        if (!latestYear.has(team.manager_key) || year >= latestYear.get(team.manager_key)) {{
          if (team.owner_display) manager.display = team.owner_display;
          latestYear.set(team.manager_key, year);
        }}
      }});

      standingRows.forEach((row) => {{
        if (!row.manager_key) return;
        const manager = ensureManager(row.manager_key);
        manager.wins += Number(row.wins || 0);
        manager.losses += Number(row.losses || 0);
        manager.ties += Number(row.ties || 0);
        manager.points_for += Number(row.points_for || 0);
        manager.points_against += Number(row.points_against || 0);
        if (row.is_champion) {{
          manager.championships += 1;
          manager.title_years.add(Number(row.year || 0));
        }}
        if (row.final_rank != null) manager.ranks.push(Number(row.final_rank));
      }});

      playoffMatchups.forEach((matchup) => {{
        const homeKey = matchup.home?.manager_key;
        const awayKey = matchup.away?.manager_key;
        if (!homeKey || !awayKey) return;
        const home = ensureManager(homeKey);
        const away = ensureManager(awayKey);
        const homeScore = Number(matchup.home?.score || 0);
        const awayScore = Number(matchup.away?.score || 0);
        const year = Number(matchup.year || 0);
        home.playoff_appearance_years.add(year);
        away.playoff_appearance_years.add(year);
        if (homeScore > awayScore) {{
          home.playoff_wins += 1;
          away.playoff_losses += 1;
        }} else if (awayScore > homeScore) {{
          away.playoff_wins += 1;
          home.playoff_losses += 1;
        }} else {{
          home.playoff_ties += 1;
          away.playoff_ties += 1;
        }}
      }});

      return [...managers.values()]
        .map((manager) => {{
          const aliasRows = [...manager.aliases.entries()]
            .map(([name, years]) => {{
              const sortedYears = [...years].sort((a, b) => a - b);
              return {{
                name,
                years: sortedYears,
                latest_year: sortedYears[sortedYears.length - 1] || 0,
              }};
            }})
            .sort((a, b) => b.latest_year - a.latest_year || a.name.localeCompare(b.name));

          const currentAlias = aliasRows[0]?.name || "-";
          const otherAliases = aliasRows.slice(1);
          const avgRank = manager.ranks.length
            ? manager.ranks.reduce((sum, value) => sum + value, 0) / manager.ranks.length
            : null;

          return {{
            ...manager,
            season_count: manager.seasons.size,
            aliases: aliasRows,
            current_alias: currentAlias,
            other_aliases: otherAliases,
            title_years: [...manager.title_years].filter((year) => Number.isFinite(year)).sort((a, b) => a - b),
            postseason_appearance_years: [...manager.playoff_appearance_years]
              .filter((year) => Number.isFinite(year))
              .sort((a, b) => a - b),
            postseason_appearances: manager.playoff_appearance_years.size,
            record: recordCell(manager.wins, manager.losses, manager.ties),
            win_pct: pctValue(manager.wins, manager.losses, manager.ties),
            avg_rank: avgRank,
          }};
        }})
        .sort((a, b) => b.championships - a.championships || b.wins - a.wins || b.points_for - a.points_for || a.display.localeCompare(b.display));
    }}

    function h2hForRange(state) {{
      const rows = new Map();
      const matchups = filterByRange(DATA.matchups, state, (row) => row.year)
        .filter((matchup) => matchup.stage === "regular" && matchup.completed && matchup.home && matchup.away);

      matchups.forEach((matchup) => {{
        const homeKey = matchup.home?.manager_key;
        const awayKey = matchup.away?.manager_key;
        if (!homeKey || !awayKey || homeKey === awayKey) return;
        const pair = [homeKey, awayKey].sort();
        const pairKey = pair.join("::");
        if (!rows.has(pairKey)) {{
          rows.set(pairKey, {{
            manager_a_key: pair[0],
            manager_b_key: pair[1],
            games: 0,
            ties: 0,
            manager_a_wins: 0,
            manager_b_wins: 0,
            points_a: 0,
            points_b: 0,
          }});
        }}
        const row = rows.get(pairKey);
        const homeScore = Number(matchup.home.score || 0);
        const awayScore = Number(matchup.away.score || 0);

        row.games += 1;
        if (homeKey === row.manager_a_key) {{
          row.points_a += homeScore;
          row.points_b += awayScore;
          if (homeScore > awayScore) row.manager_a_wins += 1;
          else if (awayScore > homeScore) row.manager_b_wins += 1;
          else row.ties += 1;
        }} else {{
          row.points_a += awayScore;
          row.points_b += homeScore;
          if (awayScore > homeScore) row.manager_a_wins += 1;
          else if (homeScore > awayScore) row.manager_b_wins += 1;
          else row.ties += 1;
        }}
      }});

      return [...rows.values()]
        .map((row) => ({{
          ...row,
          points_a: Number(row.points_a.toFixed(1)),
          points_b: Number(row.points_b.toFixed(1)),
          record: row.ties ? row.manager_a_wins + "-" + row.manager_b_wins + "-" + row.ties : row.manager_a_wins + "-" + row.manager_b_wins,
        }}))
        .sort((a, b) => b.games - a.games || managerName(a.manager_a_key).localeCompare(managerName(b.manager_a_key)) || managerName(a.manager_b_key).localeCompare(managerName(b.manager_b_key)));
    }}

    function h2hMatrixTable(rows) {{
      const managerKeys = [...new Set(
        rows.flatMap((row) => [row.manager_a_key, row.manager_b_key]).filter((key) => !!key)
      )].sort((a, b) => managerName(a).localeCompare(managerName(b)));

      const cellMap = new Map();
      rows.forEach((row) => {{
        const ties = Number(row.ties || 0);
        const aWins = Number(row.manager_a_wins || 0);
        const bWins = Number(row.manager_b_wins || 0);
        const ab = ties ? aWins + "-" + bWins + "-" + ties : aWins + "-" + bWins;
        const ba = ties ? bWins + "-" + aWins + "-" + ties : bWins + "-" + aWins;
        cellMap.set(row.manager_a_key + "::" + row.manager_b_key, ab);
        cellMap.set(row.manager_b_key + "::" + row.manager_a_key, ba);
      }});

      const wrap = el("div", "table-wrap h2h-matrix-wrap");
      const tbl = document.createElement("table");
      tbl.className = "h2h-matrix";

      const thead = document.createElement("thead");
      const headRow = document.createElement("tr");
      const corner = el("th", "h2h-corner", "Manager");
      headRow.append(corner);
      managerKeys.forEach((key) => {{
        const th = el("th", "", managerName(key));
        th.title = managerName(key);
        headRow.append(th);
      }});
      thead.append(headRow);

      const tbody = document.createElement("tbody");
      managerKeys.forEach((rowKey) => {{
        const tr = document.createElement("tr");
        const rowHead = el("th", "h2h-row-head", managerName(rowKey));
        rowHead.title = managerName(rowKey);
        tr.append(rowHead);

        managerKeys.forEach((colKey) => {{
          const td = el("td", "h2h-cell");
          if (rowKey === colKey) {{
            td.classList.add("h2h-diag");
            td.textContent = "—";
          }} else {{
            const value = cellMap.get(rowKey + "::" + colKey) || "";
            td.textContent = value || "";
          }}
          tr.append(td);
        }});

        tbody.append(tr);
      }});

      tbl.append(thead, tbody);
      wrap.append(tbl);
      return wrap;
    }}

    function recordsForRange(state) {{
      const standings = filterByRange(DATA.standings, state, (row) => row.year);
      const matchups = filterByRange(DATA.matchups, state, (row) => row.year);
      const records = [];

      function add(category, label, value, detail, year) {{
        records.push({{ category, label, value, detail, year }});
      }}

      if (standings.length) {{
        const bestPoints = [...standings].sort((a, b) => Number(b.points_for || 0) - Number(a.points_for || 0))[0];
        const lowCandidates = standings.filter((row) => Number(row.points_for || 0) > 0);
        const worstPoints = lowCandidates.length ? [...lowCandidates].sort((a, b) => Number(a.points_for || 0) - Number(b.points_for || 0))[0] : null;
        const bestRecord = [...standings].sort((a, b) => Number(b.win_pct || 0) - Number(a.win_pct || 0) || Number(b.wins || 0) - Number(a.wins || 0) || Number(b.points_for || 0) - Number(a.points_for || 0))[0];

        add("Season", "Best season points", Number(bestPoints.points_for || 0).toFixed(1), bestPoints.team_name + " (" + managerName(bestPoints.manager_key) + ")", bestPoints.year);
        if (worstPoints) {{
          add("Season", "Lowest season points", Number(worstPoints.points_for || 0).toFixed(1), worstPoints.team_name + " (" + managerName(worstPoints.manager_key) + ")", worstPoints.year);
        }}
        add("Season", "Best record", bestRecord.record, bestRecord.team_name + " (" + Number(bestRecord.win_pct || 0).toFixed(3) + ")", bestRecord.year);
      }}

      const sideScores = [];
      const gameScores = [];
      matchups.forEach((matchup) => {{
        const home = matchup.home;
        const away = matchup.away;
        if (home && Number(home.score || 0) > 0) {{
          sideScores.push({{ year: matchup.year, week: matchup.week, team_name: home.team_name, score: Number(home.score || 0) }});
        }}
        if (away && Number(away.score || 0) > 0) {{
          sideScores.push({{ year: matchup.year, week: matchup.week, team_name: away.team_name, score: Number(away.score || 0) }});
        }}
        if (home && away && matchup.margin != null) gameScores.push(matchup);
      }});

      if (sideScores.length) {{
        const high = [...sideScores].sort((a, b) => b.score - a.score)[0];
        const low = [...sideScores].sort((a, b) => a.score - b.score)[0];
        add("Weekly", "Highest weekly score", high.score.toFixed(1), high.team_name + " vs week " + high.week, high.year);
        add("Weekly", "Lowest weekly score", low.score.toFixed(1), low.team_name + " vs week " + low.week, low.year);
      }}

      if (gameScores.length) {{
        const largest = [...gameScores].sort((a, b) => Number(b.margin || 0) - Number(a.margin || 0))[0];
        const closest = [...gameScores].sort((a, b) => Number(a.margin || 0) - Number(b.margin || 0))[0];
        const detail = (matchup) => matchup.away.team_name + " " + Number(matchup.away.score || 0).toFixed(1) + " at " + matchup.home.team_name + " " + Number(matchup.home.score || 0).toFixed(1) + ", week " + matchup.week;
        add("Game", "Largest margin", Number(largest.margin || 0).toFixed(1), detail(largest), largest.year);
        add("Game", "Closest game", Number(closest.margin || 0).toFixed(1), detail(closest), closest.year);

        const losses = [];
        gameScores.forEach((matchup) => {{
          const home = matchup.home;
          const away = matchup.away;
          if (Number(home.score || 0) > Number(away.score || 0)) losses.push({{ ...away, year: matchup.year, week: matchup.week }});
          else if (Number(away.score || 0) > Number(home.score || 0)) losses.push({{ ...home, year: matchup.year, week: matchup.week }});
        }});
        if (losses.length) {{
          const bestLoss = [...losses].sort((a, b) => Number(b.score || 0) - Number(a.score || 0))[0];
          add("Game", "Best losing score", Number(bestLoss.score || 0).toFixed(1), bestLoss.team_name + " in week " + bestLoss.week, bestLoss.year);
        }}
      }}

      return records;
    }}

    function renderOverview() {{
      const root = byId("overview");
      const champions = DATA.standings.filter((row) => row.is_champion).sort((a, b) => b.year - a.year);
      const topAvgManagers = [...DATA.managers]
        .map((manager) => ({{ ...manager, games: Number(manager.wins || 0) + Number(manager.losses || 0) + Number(manager.ties || 0), avg_ppg: managerAvgPpg(manager) }}))
        .filter((manager) => manager.games > 0)
        .sort((a, b) => b.avg_ppg - a.avg_ppg || b.points_for - a.points_for || b.wins - a.wins)
        .slice(0, 5);
      const seasonScoringRows = DATA.seasons
        .map((season) => {{
          const sideScores = DATA.matchups
            .filter((matchup) => matchup.year === season.year && matchup.stage === "regular" && matchup.completed && matchup.home && matchup.away)
            .flatMap((matchup) => [Number(matchup.home.score || 0), Number(matchup.away.score || 0)])
            .filter((score) => Number.isFinite(score));
          const avgScore = sideScores.length
            ? sideScores.reduce((sum, score) => sum + score, 0) / sideScores.length
            : 0;
          return {{
            year: season.year,
            avg_score: avgScore,
          }};
        }})
        .sort((a, b) => a.year - b.year);
      const matchupCount = regularMatchups().length;
      const [avgPpgMin, avgPpgMax] = numericExtent(topAvgManagers.map((manager) => manager.avg_ppg));
      const [pfMin, pfMax] = numericExtent(topAvgManagers.map((manager) => manager.points_for));
      const [champPointsMin, champPointsMax] = numericExtent(champions.map((row) => row.points_for));
      const grid = el("div", "grid");
      grid.append(
        card("Seasons", DATA.seasons.length, DATA.metadata.start_year + "-" + DATA.metadata.end_year),
        card("Managers", DATA.managers.length, "Owner-first identities"),
        card("Regular matchups", matchupCount, "Completed regular-season games")
      );

      const columns = el("div", "columns");
      const left = el("div", "panel");
      left.append(el("h2", "", "Season Scoring Trend"));
      left.append(barChart(seasonScoringRows.map((row) => ({{ label: yearBubble(row.year), value: row.avg_score, display: row.avg_score.toFixed(1) }})), "blue"));
      left.append(el("div", "muted", "Average regular-season team score by season."));

      const right = el("div", "panel");
      right.classList.add("leader-table");
      right.append(el("h2", "", "Top 5 Manager Avg PPG"));
      right.append(table(
        [
          {{ label: "Manager" }},
          {{ label: "Avg PPG", num: true }},
          {{ label: "Games", num: true }},
          {{ label: "Record" }},
          {{ label: "PF", num: true }},
          {{ label: "Titles", num: true }}
        ],
        topAvgManagers.map((manager) => [
          managerLabel(manager.manager_key),
          heatBadge(manager.avg_ppg, avgPpgMin, avgPpgMax, 2),
          manager.games,
          manager.record,
          heatBadge(manager.points_for, pfMin, pfMax, 1),
          manager.championships > 0 ? metricBadge(String(manager.championships), "good") : metricBadge("0", "mid")
        ])
      ));
      columns.append(left, right);

      const champPanel = el("div", "panel");
      champPanel.append(el("h2", "", "Champions"));
      champPanel.append(table(
        [
          {{ label: "Year", num: true }},
          {{ label: "Team" }},
          {{ label: "Manager" }},
          {{ label: "Record" }},
          {{ label: "Points", num: true }}
        ],
        champions.map((row) => [yearBubble(row.year), row.team_name + " 🏆", managerLabel(row.manager_key), row.record, heatBadge(row.points_for, champPointsMin, champPointsMax, 1)])
      ));
      root.replaceChildren(grid, columns, champPanel);
    }}

    function renderSeasons() {{
      const root = byId("seasons");
      const selected = root.dataset.year || String(DATA.metadata.end_year);
      const controls = el("div", "controls");
      controls.append(el("label", "", "Season"));
      const select = document.createElement("select");
      options(select, DATA.seasons.map((s) => [s.year, String(s.year)]), selected);
      select.addEventListener("change", () => {{
        root.dataset.year = select.value;
        renderSeasons();
      }});
      controls.append(select);

      const year = Number(select.value);
      const season = DATA.seasons.find((item) => item.year === year);
      const rows = DATA.standings.filter((row) => row.year === year);
      const [pfMin, pfMax] = numericExtent(rows.map((row) => row.points_for));
      const [paMin, paMax] = numericExtent(rows.map((row) => row.points_against));
      const grid = el("div", "grid");
      grid.append(
        card("Teams", season?.team_count || 0, "League size"),
        card("Regular season", season?.reg_season_count || 0, "Matchup periods"),
        card("Playoff teams", season?.playoff_team_count || 0, "ESPN setting"),
        card("Champion", rows.find((row) => row.is_champion)?.team_name || "N/A", "League title winner")
      );

      const panel = el("div", "panel");
      panel.append(el("h2", "", year + " Standings"));
      panel.append(table(
        [
          {{ label: "Rank", num: true }},
          {{ label: "Team" }},
          {{ label: "Manager" }},
          {{ label: "Title" }},
          {{ label: "Record" }},
          {{ label: "Win Pct", num: true }},
          {{ label: "PF", num: true }},
          {{ label: "PA", num: true }},
          {{ label: "Moves", num: true }}
        ],
        rows.map((row, index) => {{
          const rank = row.final_rank || row.playoff_seed || index + 1;
          return {{
            className: row.is_champion ? "champion-row" : "",
            cells: [
              rankBadge(rank),
              row.team_name,
              managerLabel(row.manager_key),
              titleBadge(row.is_champion),
              row.record,
              winPctBadge(row.win_pct),
              heatBadge(row.points_for, pfMin, pfMax, 1),
              heatBadge(row.points_against, paMin, paMax, 1, true),
              row.acquisitions + row.drops + row.trades
            ]
          }};
        }})
      ));
      root.replaceChildren(controls, grid, panel);
    }}

    function renderManagers() {{
      const root = byId("managers");
      const search = root.dataset.search || "";
      const controls = el("div", "controls");
      const rangeState = attachRangeControls(controls, root, renderManagers);
      controls.append(el("label", "", "Search"));
      const input = document.createElement("input");
      input.type = "search";
      input.value = search;
      input.placeholder = "Manager or team alias";
      input.addEventListener("input", () => {{
        root.dataset.search = input.value;
        renderManagers();
      }});
      controls.append(input);

      const needle = search.trim().toLowerCase();
      const managers = managersForRange(rangeState).filter((manager) => {{
        const aliasText = manager.aliases.map((alias) => alias.name).join(" ").toLowerCase();
        return !needle || manager.display.toLowerCase().includes(needle) || aliasText.includes(needle);
      }});
      const [pfMin, pfMax] = numericExtent(managers.map((manager) => manager.points_for));
      const [paMin, paMax] = numericExtent(managers.map((manager) => manager.points_against));

      const panel = el("div", "panel");
      panel.append(el("h2", "", "Manager History"));
      panel.append(table(
        [
          {{ label: "Manager" }},
          {{ label: "Alias" }},
          {{ label: "Seasons", num: true }},
          {{ label: "Titles" }},
          {{ label: "Postseason Apps", num: true }},
          {{ label: "Playoff Rec" }},
          {{ label: "Avg Rank", num: true }},
          {{ label: "Record" }},
          {{ label: "Win Pct", num: true }},
          {{ label: "PF", num: true }},
          {{ label: "PA", num: true }}
        ],
        managers.map((manager) => {{
          const avgRank = Number(manager.avg_rank || 0);
          const avgRankCell = manager.avg_rank == null
            ? el("span", "muted", "-")
            : metricBadge(avgRank.toFixed(2), avgRank <= 4 ? "good" : avgRank <= 6 ? "mid" : "bad");
          return [
            managerLabel(manager.manager_key),
            {{ content: aliasHoverCell(manager.current_alias, manager.other_aliases), sortValue: manager.current_alias.toLowerCase() }},
            {{ content: manager.season_count, sortValue: manager.season_count }},
            {{ content: titlesEmojiCell(manager.championships, manager.title_years), sortValue: manager.championships }},
            {{ content: postseasonAppearancesCell(manager.postseason_appearances, manager.postseason_appearance_years), sortValue: manager.postseason_appearances }},
            {{ content: playoffRecordCell(manager), sortValue: pctValue(manager.playoff_wins, manager.playoff_losses, manager.playoff_ties) }},
            {{ content: avgRankCell, sortValue: manager.avg_rank == null ? 999 : manager.avg_rank }},
            manager.record,
            {{ content: winPctBadge(manager.win_pct), sortValue: manager.win_pct }},
            {{ content: heatBadge(manager.points_for, pfMin, pfMax, 1), sortValue: manager.points_for }},
            {{ content: heatBadge(manager.points_against, paMin, paMax, 1, true), sortValue: manager.points_against }}
          ];
        }})
      ));
      root.replaceChildren(controls, panel);
    }}

    function renderMatchups() {{
      const root = byId("matchups");
      const controls = el("div", "controls");
      const baseRows = DATA.matchups.filter((matchup) => matchup.home && matchup.away && matchup.completed);
      const seasonOptions = [...new Set(baseRows.map((matchup) => Number(matchup.year || 0)).filter((year) => year > 0))].sort((a, b) => b - a);
      let selectedSeason = Number(root.dataset.season || seasonOptions[0] || DATA.metadata.end_year || 0);
      if (!seasonOptions.includes(selectedSeason)) {{
        selectedSeason = seasonOptions[0] || selectedSeason;
      }}
      root.dataset.season = String(selectedSeason);

      let selectedScope = root.dataset.scope || "regular_week";
      const scopeOptions = [
        ["regular_week", "Regular Season (One Week)"],
        ["playoff_all", "Playoff Bracket (All Games)"],
        ["consolation_all", "Consolation Ladder (All Games)"],
      ];
      if (!scopeOptions.some(([value]) => value === selectedScope)) {{
        selectedScope = "regular_week";
      }}
      root.dataset.scope = selectedScope;

      controls.append(el("label", "", "Season"));
      const seasonSelect = document.createElement("select");
      options(seasonSelect, seasonOptions.map((year) => [year, String(year)]), selectedSeason);
      seasonSelect.addEventListener("change", () => {{
        root.dataset.season = seasonSelect.value;
        renderMatchups();
      }});
      controls.append(seasonSelect);

      controls.append(el("label", "", "Scope"));
      const scopeSelect = document.createElement("select");
      options(scopeSelect, scopeOptions, selectedScope);
      scopeSelect.addEventListener("change", () => {{
        root.dataset.scope = scopeSelect.value;
        renderMatchups();
      }});
      controls.append(scopeSelect);

      const seasonRows = baseRows.filter((matchup) => Number(matchup.year || 0) === selectedSeason);
      const regularWeeks = [...new Set(
        seasonRows
          .filter((matchup) => matchupBucket(matchup) === "regular")
          .map((matchup) => Number(matchup.week || 0))
          .filter((week) => week > 0)
      )].sort((a, b) => a - b);
      let selectedWeek = Number(root.dataset.week || (regularWeeks.length ? regularWeeks[regularWeeks.length - 1] : 0));
      if (!regularWeeks.includes(selectedWeek)) {{
        selectedWeek = regularWeeks.length ? regularWeeks[regularWeeks.length - 1] : 0;
      }}
      root.dataset.week = String(selectedWeek);

      if (selectedScope === "regular_week") {{
        controls.append(el("label", "", "Week"));
        const weekSelect = document.createElement("select");
        const weekChoices = regularWeeks.length ? regularWeeks.map((week) => [week, "Week " + week]) : [[0, "No regular weeks"]];
        options(weekSelect, weekChoices, selectedWeek);
        if (!regularWeeks.length) weekSelect.disabled = true;
        weekSelect.addEventListener("change", () => {{
          root.dataset.week = weekSelect.value;
          renderMatchups();
        }});
        controls.append(weekSelect);
      }}

      let selectedManager = root.dataset.manager || "all";
      if (selectedManager !== "all" && !DATA.managers.some((manager) => manager.manager_key === selectedManager)) {{
        selectedManager = "all";
      }}
      root.dataset.manager = selectedManager;

      controls.append(el("label", "", "Manager"));
      const managerSelect = document.createElement("select");
      options(managerSelect, [["all", "All managers"], ...DATA.managers.map((m) => [m.manager_key, m.display])], selectedManager);
      managerSelect.addEventListener("change", () => {{
        root.dataset.manager = managerSelect.value;
        renderMatchups();
      }});
      controls.append(managerSelect);

      const matchups = seasonRows
        .filter((matchup) => {{
          const bucket = matchupBucket(matchup);
          if (selectedScope === "regular_week") {{
            return bucket === "regular" && Number(matchup.week || 0) === selectedWeek;
          }}
          if (selectedScope === "playoff_all") return bucket === "playoff";
          if (selectedScope === "consolation_all") return bucket === "consolation";
          return false;
        }})
        .filter((matchup) => {{
          if (selectedManager === "all") return true;
          return matchup.home.manager_key === selectedManager || matchup.away.manager_key === selectedManager;
        }});

      const panel = el("div", "panel");
      panel.append(el("h2", "", "Matchup Results · " + selectedSeason));
      if (!matchups.length) {{
        panel.append(el("div", "matchup-empty", "No matchups for selected scope."));
        root.replaceChildren(controls, panel);
        return;
      }}

      const board = el("div", "matchup-board");
      const section = el("section", "matchup-year");
      const head = el("div", "matchup-year-head");
      head.append(yearBubble(selectedSeason));
      head.append(el("span", "matchup-year-count", intFmt(matchups.length) + " games"));
      section.append(head);

      if (selectedScope === "playoff_all") {{
        const roundGroups = new Map();
        matchups
          .slice()
          .sort((a, b) => Number(a.week || 0) - Number(b.week || 0))
          .forEach((matchup) => {{
            const week = Number(matchup.week || 0);
            const label = playoffRoundLabel(selectedSeason, week);
            const groupKey = String(week) + "::" + label;
            if (!roundGroups.has(groupKey)) {{
              roundGroups.set(groupKey, {{ week, label, rows: [] }});
            }}
            roundGroups.get(groupKey).rows.push(matchup);
          }});

        [...roundGroups.values()]
          .sort((a, b) => a.week - b.week)
          .forEach((group) => {{
            const block = el("div", "matchup-round");
            const title = el("div", "matchup-round-title");
            title.append(el("span", "", "Playoff " + group.label));
            block.append(title);
            const grid = el("div", "matchup-grid");
            group.rows.forEach((matchup) => grid.append(matchupCard(matchup, {{
              roundLabel: group.label,
              showWeekChip: true,
              showStageChip: false,
              showRoundChip: false,
            }})));
            block.append(grid);
            section.append(block);
          }});
      }} else if (selectedScope === "consolation_all") {{
        const byWeek = new Map();
        matchups.forEach((matchup) => {{
          const week = Number(matchup.week || 0);
          if (!byWeek.has(week)) byWeek.set(week, []);
          byWeek.get(week).push(matchup);
        }});

        [...byWeek.keys()]
          .sort((a, b) => a - b)
          .forEach((week) => {{
            const block = el("div", "matchup-round");
            const title = el("div", "matchup-round-title");
            title.append(el("span", "", "Consolation · Week " + week));
            block.append(title);
            const grid = el("div", "matchup-grid");
            byWeek.get(week).forEach((matchup) => grid.append(matchupCard(matchup, {{
              showWeekChip: false,
              showStageChip: false,
              showRoundChip: false,
            }})));
            block.append(grid);
            section.append(block);
          }});
      }} else {{
        const block = el("div", "matchup-round");
        const title = el("div", "matchup-round-title");
        title.append(el("span", "", "Regular Season · Week " + selectedWeek));
        block.append(title);
        const grid = el("div", "matchup-grid");
        matchups
          .slice()
          .sort((a, b) => Number(a.matchup_ref?.split(":")[2] || 0) - Number(b.matchup_ref?.split(":")[2] || 0))
          .forEach((matchup) => grid.append(matchupCard(matchup, {{
            showWeekChip: false,
            showStageChip: false,
            showRoundChip: false,
          }})));
        block.append(grid);
        section.append(block);
      }}

      board.append(section);

      panel.append(board);
      root.replaceChildren(controls, panel);
    }}

    function renderRecords() {{
      const root = byId("records");
      const controls = el("div", "controls");
      const rangeState = attachRangeControls(controls, root, renderRecords);
      const scopedRecords = recordsForRange(rangeState);
      const scopedH2h = h2hForRange(rangeState);

      const recordPanel = el("div", "panel");
      recordPanel.append(el("h2", "", "League Records"));
      recordPanel.append(table(
        [
          {{ label: "Category" }},
          {{ label: "Record" }},
          {{ label: "Value", num: true }},
          {{ label: "Detail" }},
          {{ label: "Year", num: true }}
        ],
        scopedRecords.map((record) => [record.category, record.label, record.value, record.detail, yearBubble(record.year)])
      ));

      const h2hPanel = el("div", "panel");
      h2hPanel.append(el("h2", "", "Regular-Season Head-to-Head"));
      if (scopedH2h.length) {{
        h2hPanel.append(h2hMatrixTable(scopedH2h));
      }} else {{
        h2hPanel.append(el("div", "matchup-empty", "No regular-season head-to-head games for selected range."));
      }}
      root.replaceChildren(controls, recordPanel, h2hPanel);
    }}

    function activate(tab) {{
      document.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
      document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === tab));
    }}

    function init() {{
      byId("pageTitle").textContent = "The History of True";
      byId("pageMeta").textContent = DATA.metadata.start_year + "-" + DATA.metadata.end_year;
      byId("generatedAt").textContent = "Generated " + new Date(DATA.metadata.generated_at).toLocaleString();
      inferBracketTypesFromSeeds();
      document.querySelectorAll(".tab").forEach((button) => {{
        button.addEventListener("click", () => activate(button.dataset.tab));
      }});
      renderOverview();
      renderSeasons();
      renderManagers();
      renderMatchups();
      renderRecords();
    }}

    init();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the True League static historical dashboard.")
    parser.add_argument("--league-id", type=int, default=DEFAULT_LEAGUE_ID)
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--manager-map",
        default=DEFAULT_MANAGER_MAP,
        help=f"Optional JSON manager-name mapping file. Defaults to {DEFAULT_MANAGER_MAP} when present.",
    )
    parser.add_argument(
        "--no-manager-map",
        action="store_true",
        help="Ignore manager-name mapping files and use ESPN-provided display values.",
    )
    parser.add_argument(
        "--write-manager-map-template",
        metavar="PATH",
        help="Write a manager mapping template after fetching data.",
    )
    parser.add_argument(
        "--manual-overrides",
        default=DEFAULT_MANUAL_OVERRIDES,
        help=f"Optional JSON manual override file. Defaults to {DEFAULT_MANUAL_OVERRIDES} when present.",
    )
    parser.add_argument(
        "--no-manual-overrides",
        action="store_true",
        help="Ignore manual override files and use ESPN-derived champions.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")
    missing = [name for name, value in (("ESPN_S2", espn_s2), ("SWID", swid)) if not value]
    if missing:
        print(
            "Missing ESPN auth environment variable(s): "
            + ", ".join(missing)
            + ". Export ESPN_S2 and SWID, then rerun.",
            file=sys.stderr,
        )
        return 2

    try:
        manager_mappings = load_manager_mappings(None if args.no_manager_map else args.manager_map)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Could not read manager mapping file: {exc}", file=sys.stderr)
        return 2

    try:
        manual_overrides = load_manual_overrides(None if args.no_manual_overrides else args.manual_overrides)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Could not read manual override file: {exc}", file=sys.stderr)
        return 2

    try:
        available_years = discover_available_years(
            league_id=args.league_id,
            espn_s2=espn_s2,
            swid=swid,
        )
    except ESPNAccessDenied as exc:
        print(f"ESPN access denied during year discovery: {exc}", file=sys.stderr)
        return 1
    except (ESPNUnknownError, requests.RequestException) as exc:
        print(f"ESPN API error during year discovery: {exc}", file=sys.stderr)
        return 1

    try:
        start_year, end_year, selected_years = resolve_selected_years(
            available_years=available_years,
            requested_start=args.start_year,
            requested_end=args.end_year,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        data = build_bundle(
            league_id=args.league_id,
            title=args.title,
            start_year=start_year,
            end_year=end_year,
            years=selected_years,
            available_years=available_years,
            espn_s2=espn_s2,
            swid=swid,
            manager_mappings=manager_mappings,
            manual_overrides=manual_overrides,
        )
    except ESPNAccessDenied as exc:
        print(f"ESPN access denied: {exc}", file=sys.stderr)
        return 1
    except ESPNInvalidLeague as exc:
        print(f"Invalid ESPN league: {exc}", file=sys.stderr)
        return 1
    except ESPNUnknownError as exc:
        print(f"ESPN API error: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    output.write_text(render_html(data), encoding="utf-8")
    if args.write_manager_map_template:
        write_manager_mapping_template(data, args.write_manager_map_template)
        print(f"Wrote manager mapping template to {args.write_manager_map_template}.")
    print(f"Wrote {output} with {len(data['seasons'])} seasons, {len(data['managers'])} managers, and {len(data['matchups'])} matchups.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
