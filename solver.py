#!/usr/bin/env python3
"""
DSO 429 Homework 3: The Hourly Quiz solver.

Fetches questions.csv from the course repo, solves every round that does not
yet have an answer file (or whose answer file still has flagged questions and
is inside the 3-hour correction window), and writes answers/round-XXXX.json.

Every question type seen so far is plain business arithmetic, so each one is
matched to a template with a regex and computed exactly. If a question matches
no template, or no option matches the computed value, the question is sent to
Claude as a fallback (when ANTHROPIC_API_KEY is set) and recorded in
needs_review.json so a later run can retry it.

Usage:
    python3 solver.py                 # fetch, solve missing rounds, write files
    python3 solver.py --validate      # score rounds 1-48 against practice_keys.csv
    python3 solver.py --force 77 78   # re-solve specific rounds
    python3 solver.py --local q.csv   # use a local questions file instead of fetching
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
ANSWERS_DIR = REPO_DIR / "answers"
REVIEW_FILE = REPO_DIR / "needs_review.json"

QUESTIONS_URL = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/questions.csv"
KEYS_URL = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/practice_keys.csv"

FIRST_GRADED_ROUND = 49
CORRECTION_WINDOW_HOURS = 3

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


# ----------------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------------

def num(s):
    """'$1,234.56' / '1,234' / '-15.1%' / '923 units' -> float"""
    s = s.strip().replace("$", "").replace(",", "").replace("%", "")
    s = re.sub(r"[a-zA-Z ]+$", "", s)
    return float(s)


def parse_date(s):
    d, m, y = s.strip().split()
    return datetime(int(y), MONTHS[m], int(d))


def fmt_date(d):
    return f"{d.day} {d.strftime('%B')} {d.year}"


# ----------------------------------------------------------------------------
# Templates. Each returns a computed answer: a float (matched to the closest
# numeric option) or a string (matched exactly).
# ----------------------------------------------------------------------------

def t_compound(m):
    p, r, n = num(m[1]), num(m[2]), int(m[3])
    return p * (1 + r / 100) ** n


def t_margin(m):
    rev, cogs = num(m[1]), num(m[2])
    return (rev - cogs) / rev * 100


def t_series(m):
    xs = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m[1])]
    diffs = [b - a for a, b in zip(xs, xs[1:])]
    if all(abs(d - diffs[0]) < 1e-9 for d in diffs):
        return xs[-1] + diffs[0]
    if all(x != 0 for x in xs):
        ratios = [b / a for a, b in zip(xs, xs[1:])]
        if all(abs(r - ratios[0]) < 1e-9 for r in ratios):
            return xs[-1] * ratios[0]
    d2 = [b - a for a, b in zip(diffs, diffs[1:])]
    if d2 and all(abs(d - d2[0]) < 1e-9 for d in d2):
        return xs[-1] + diffs[-1] + d2[0]
    return None


def t_discount(m):
    return num(m[1]) * (1 - num(m[2]) / 100)


def t_pct_change(m):
    a, b = num(m[1]), num(m[2])
    return (b - a) / a * 100


def t_settle(m):
    return fmt_date(parse_date(m[1]) + timedelta(days=int(m[2])))


def t_breakeven(m):
    fixed, price, var = num(m[1]), num(m[2]), num(m[3])
    return fixed / (price - var)


def t_ratio_split(m):
    total, a, b = num(m[1]), num(m[2]), num(m[3])
    return total * a / (a + b)


def t_weighted_avg(m):
    n1, a1, n2, a2 = num(m[1]), num(m[2]), num(m[3]), num(m[4])
    return (n1 * a1 + n2 * a2) / (n1 + n2)


def t_currency(m):
    return num(m[1]) * num(m[2])


def t_revenue(m):
    return num(m[1]) * num(m[2])


def t_probability(m):
    b, t, a = num(m[1]), num(m[2]), num(m[3])
    return b / (b + t + a) * 100


MONEY = r"(\$[\d,]+(?:\.\d+)?)"
PCT = r"([\d.]+)%"
INT = r"([\d,]+)"
DATE = r"(\d{1,2} [A-Z][a-z]+ \d{4})"

TEMPLATES = [
    ("compound", rf"An investment of {MONEY} grows at {PCT} compounded annually\. What is its value after (\d+) years?", t_compound),
    ("margin", rf"A product line generates {MONEY} in revenue against {MONEY} in cost of goods sold\. What is the gross margin\?", t_margin),
    ("series", r"A monthly headcount series runs ([\d, .\-]+)\. If the pattern continues, what is the next value\?", t_series),
    ("discount", rf"An item listed at {MONEY} is discounted by {PCT}\. What is the sale price\?", t_discount),
    ("pct_change", rf"Quarterly revenue moved from {MONEY} to {MONEY}\. What is the percentage change\?", t_pct_change),
    ("settle", rf"A contract signed on {DATE} has an? (\d+)-day settlement period\. On what date does it settle\?", t_settle),
    ("breakeven", rf"Fixed costs are {MONEY}\. Each unit sells for {MONEY} with a variable cost of {MONEY}\. How many units must be sold to break even", t_breakeven),
    ("ratio_split", rf"A {MONEY} marketing budget is split between two channels in the ratio (\d+):(\d+)\. What does the first channel receive\?", t_ratio_split),
    ("weighted_avg", rf"Region A had {INT} orders averaging {MONEY}; region B had {INT} orders averaging {MONEY}\. What is the overall average order value\?", t_weighted_avg),
    ("currency", rf"An invoice for {MONEY} is converted to euros at ([\d.]+) euros per US dollar\. How many euros is that\?", t_currency),
    ("revenue", rf"A retailer sells {INT} units at {MONEY} each\. What is total revenue\?", t_revenue),
    ("probability", rf"A support queue holds {INT} billing tickets, {INT} technical tickets and {INT} account tickets\. If one is drawn at random, what is the probability it is a billing ticket\?", t_probability),
]
COMPILED = [(name, re.compile(pat), fn) for name, pat, fn in TEMPLATES]


def pick_option(value, options):
    """options: dict letter -> text. Returns (letter, confident)."""
    if value is None:
        return None, False
    if isinstance(value, str):
        for k, v in options.items():
            if v.strip() == value:
                return k, True
        return None, False
    best, best_err = None, None
    for k, v in options.items():
        try:
            ov = num(v)
        except ValueError:
            continue
        err = abs(ov - value)
        if best_err is None or err < best_err:
            best, best_err = k, err
    if best is None:
        return None, False
    # Tolerance: options are rounded to 2dp (money) or 1dp (percent) or whole
    # units; anything within 1% of the value or 0.06 absolute is a match.
    tol = max(0.06, abs(value) * 0.01)
    return best, best_err <= tol


def solve_question(q):
    """q: csv row dict. Returns (letter, template_name, confident)."""
    text = q["question_text"].strip()
    options = {L: q[f"option_{L.lower()}"] for L in "ABCD"}
    for name, rx, fn in COMPILED:
        m = rx.match(text)
        if m:
            try:
                value = fn(m)
            except Exception:
                value = None
            letter, ok = pick_option(value, options)
            if ok:
                return letter, name, True
            return letter, name, False
    return None, "unmatched", False


# ----------------------------------------------------------------------------
# Claude fallback for anything the templates cannot handle
# ----------------------------------------------------------------------------

def claude_fallback(q):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    options = "\n".join(f"{L}) {q[f'option_{L.lower()}']}" for L in "ABCD")
    prompt = (
        "Solve this multiple-choice business arithmetic question. Work it out "
        "step by step, then on the final line write only the letter of the "
        "correct option (A, B, C or D).\n\n"
        f"{q['question_text']}\n{options}"
    )
    body = json.dumps({
        "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        text = "".join(b.get("text", "") for b in data.get("content", []))
        letters = re.findall(r"\b([ABCD])\b", text.strip().splitlines()[-1])
        return letters[-1] if letters else None
    except Exception as e:
        print(f"  fallback failed: {e}", file=sys.stderr)
        return None


# ----------------------------------------------------------------------------
# Round handling
# ----------------------------------------------------------------------------

def fetch(url, attempts=4):
    last = None
    for i in range(attempts):
        try:
            # raw.githubusercontent.com caches for ~5 minutes; a unique query
            # string skips the stale copy so new rounds show up right away.
            busted = f"{url}?t={int(time.time() * 1000)}"
            req = urllib.request.Request(busted, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8")
        except Exception as e:
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


def load_questions(local=None):
    text = Path(local).read_text() if local else fetch(QUESTIONS_URL)
    rows = list(csv.DictReader(io.StringIO(text)))
    rounds = {}
    for r in rows:
        rounds.setdefault(int(r["round_id"]), []).append(r)
    for rid in rounds:
        rounds[rid].sort(key=lambda r: int(r["q_index"]))
    return rounds


def round_path(rid):
    return ANSWERS_DIR / f"round-{rid:04d}.json"


def load_review():
    if REVIEW_FILE.exists():
        try:
            return json.loads(REVIEW_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def solve_round(rid, qs, review):
    answers = {}
    flagged = []
    for q in qs:
        qi = int(q["q_index"])
        letter, name, ok = solve_question(q)
        if not ok:
            fb = claude_fallback(q)
            if fb:
                letter, ok = fb, True
                name = f"{name}+claude"
            flagged.append({"q_index": qi, "template": name, "question": q["question_text"],
                            "answer_used": letter or "A", "resolved_by_claude": bool(fb)})
        answers[f"q{qi}"] = letter or "A"
    # Make sure q1..q25 all exist even if the CSV is short
    for i in range(1, 26):
        answers.setdefault(f"q{i}", "A")
    answers = {f"q{i}": answers[f"q{i}"] for i in range(1, 26)}
    out = {"round_id": rid, "answers": answers}
    ANSWERS_DIR.mkdir(exist_ok=True)
    round_path(rid).write_text(json.dumps(out, indent=2) + "\n")
    unresolved = [f for f in flagged if not f["resolved_by_claude"]]
    if unresolved:
        review[str(rid)] = {"released_at": qs[0]["released_at"], "questions": unresolved}
    else:
        review.pop(str(rid), None)
    return flagged


def released_hours_ago(released_at):
    t = datetime.fromisoformat(released_at.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def main_solve(args):
    rounds = load_questions(args.local)
    review = load_review()
    todo = []
    for rid in sorted(rounds):
        if args.force and rid not in args.force:
            continue
        if args.force:
            todo.append(rid)
            continue
        if not round_path(rid).exists():
            todo.append(rid)
        elif str(rid) in review and released_hours_ago(rounds[rid][0]["released_at"]) < CORRECTION_WINDOW_HOURS:
            todo.append(rid)  # retry flagged questions while corrections still count
    if not todo:
        print(f"nothing to do: {len(rounds)} rounds present, latest is {max(rounds)}")
        return 0
    for rid in todo:
        flagged = solve_round(rid, rounds[rid], review)
        note = f"  {len(flagged)} flagged" if flagged else ""
        print(f"round {rid:4d}: written{note}")
        for f in flagged:
            print(f"    q{f['q_index']}: {f['template']} -> {f['answer_used']}  {f['question'][:70]}")
    if review:
        REVIEW_FILE.write_text(json.dumps(review, indent=2) + "\n")
    elif REVIEW_FILE.exists():
        REVIEW_FILE.unlink()
    print(f"done: {len(todo)} round(s) written, latest round is {max(rounds)}")
    return 0


def main_validate(args):
    rounds = load_questions(args.local)
    keys_text = Path(args.keys).read_text() if args.keys else fetch(KEYS_URL)
    keys = {(int(r["round_id"]), int(r["q_index"])): r["answer"].strip()
            for r in csv.DictReader(io.StringIO(keys_text))}
    total = correct = 0
    by_template = {}
    for rid in sorted(rounds):
        for q in rounds[rid]:
            k = keys.get((rid, int(q["q_index"])))
            if k is None:
                continue
            letter, name, ok = solve_question(q)
            total += 1
            bt = by_template.setdefault(name, [0, 0])
            bt[1] += 1
            if letter == k:
                correct += 1
                bt[0] += 1
            else:
                print(f"MISS r{rid} q{q['q_index']} [{name}] got {letter} key {k} ok={ok}: {q['question_text']}")
                print("     ", {L: q[f'option_{L.lower()}'] for L in 'ABCD'})
    for name, (c, n) in sorted(by_template.items()):
        print(f"{name:14s} {c}/{n}")
    print(f"TOTAL {correct}/{total}")
    return 0 if correct == total else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--local", help="local questions.csv instead of fetching")
    ap.add_argument("--keys", help="local practice_keys.csv (with --validate)")
    ap.add_argument("--force", nargs="*", type=int, help="re-solve these rounds")
    a = ap.parse_args()
    sys.exit(main_validate(a) if a.validate else main_solve(a))
