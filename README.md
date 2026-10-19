# DSO 429 Homework 3: The Hourly Quiz

Automated answers for the hourly quiz. One file per round in `answers/round-XXXX.json`.

## How it works

`solver.py` fetches the course `questions.csv`, matches each question to one of
the question templates with a regex, computes the answer exactly, and picks the
option that matches. It scored 1200/1200 on the 48 practice rounds.

If a question matches no template (or no option matches the computed value),
it falls back to Claude via the API when `ANTHROPIC_API_KEY` is set, and logs
the question in `needs_review.json`. Flagged rounds are retried on later runs
while they are still inside the 3-hour correction window.

## Automation

`.github/workflows/hourly-quiz.yml` runs the solver every 5 minutes on GitHub
Actions and commits any new answer files to `main`. It can also be triggered by
hand from the Actions tab.

## Running by hand

```
python3 solver.py              # solve anything missing
python3 solver.py --validate   # check against the practice keys
python3 solver.py --force 80   # re-solve a specific round
```
