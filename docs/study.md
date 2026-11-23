# Studying with its

It tutors you from your actual course material and keeps track of where you are, so a restart or a new day doesn't
lose it. Say "let's study for the AI midterm" and it checks the tracker, picks up where you left off and goes one
step and one question at a time.

## The study tracker

`data/study/study.json` keeps, per course:

- its exams (date, and the topics each covers), noticed in Canvas (an assignment or quiz called "Midterm",
  "Class Test 2", "Final Exam") or recorded while you study;
- each topic as not started, started or done, and the weak spots, with what's shaky;
- where you left off, your sessions, and the questions you missed.

"How far along am I?" is answered from it (a started topic counts half). The tools: `study_status`, `study_update`,
`quiz_record`, `study_reviews`, `study_session`. They only write that file, so they never ask.

## Quizzes and reviews

It quizzes you only when you ask or say yes. Questions come from your slides, one at a time (at the screen, on a
pop-up panel that updates with each question and the score); it waits for your
answer, says right or wrong with a short why, and records it. A miss comes back as a review later that day, then
after a day, three days, a week and two weeks; a right answer each time retires it, a wrong one starts it over.

## Study sessions

"Start a study session for AI" pauses the music and holds its own remarks and the spoken email and phone alerts for a
block (45 minutes unless you say otherwise), then it calls a break. Ending it saves how long it ran, what you
covered and where to pick up. Windows' Do Not Disturb is left alone.

## Reading the slides

`canvas_course_material` reads a course's files as text. A slide that's a picture or a diagram (a game tree) is
marked in that text, and `page=N` shows it that page as a picture, drawn by Windows' own PDF renderer
(`scripts/pdf_page.ps1`), so nothing needs installing.

## What it brings up on its own

Through the same gate as all its remarks (quiet hours, focus, Do Not Disturb, budgets and mutes all apply):

- an exam within `study.exam_days` days (3) with no study session in the last day and a half;
- `study.min_reviews` (3) or more missed questions due for review.

Each is a question, at most once a day for the same exam or course. Turn them off with `study.nudges: false`.

## Graded work

Graded quizzes, exams, tests and knowledge checks are yours to do. It explains the idea behind a question but never
gives the answer, picks an option, or types or clicks in one; on a practice quiz it tutors you and gives the answer
when you ask (Canvas's quiz details say which it is). In code, whatever it's told: a click that turns in an attempt
("Submit Quiz", "Finish attempt") is refused on any site, and nothing is done in a lockdown exam browser.

## Settings

```yaml
study:
  enabled: true
  nudges: true
  exam_days: 3
  min_reviews: 3
  canvas_exams: true
```

## Practice problems and walkthroughs (search and game trees)

For BFS, DFS, uniform-cost, greedy best-first, A*, minimax and alpha-beta it never traces a problem in its head:
`practice_problem` sets one (made up, or the tree or graph from your slides or homework) and shows it on a pop-up
panel without the answer; `check_practice` grades your answer by code and, when something's off, says the first step
that went wrong and shows it; `walk_through` shows the solution one step at a time on the same panel (values rising
up the tree, α and β at each node, pruned branches dashed red; or the frontier and each expansion).

The rules are your course's (ITCS 6150 activity solutions, which follow the textbook): successors in alphabetical
order, ties broken alphabetically, the goal test when a node is taken off the frontier, no duplicate states generated
in BFS and DFS, and alpha-beta pruning as soon as α ≥ β. The solvers reproduce the course's alpha-beta (15a), A* (06)
and uniform-cost (03c) solutions. Problems are kept in `data/study/problems.json` (the last 30).

### Adding a subject

Each kind of problem is a `PracticeKind` (`src/octavia/study/practice/base.py`): it makes a problem at random or reads
one the user gives (its `Given` model), says it in words without the answer, grades an `Answer` exactly with the
first wrong step, walks through the solution in `Step`s and, if it can, draws each step as SVG. A subject is a module
of kinds that calls `register(...)` for each, listed in `SUBJECTS` (`practice/__init__.py`): `graph_search.py` and
`game_trees.py` are the two so far. The tools and the problem book don't change; the tools' descriptions list every
registered kind with what it takes.

### Worksheets (working it out like on the exam)

`practice_worksheet` opens a problem in the browser as a page to write on: a box in every node of a game tree for its
value (and, for alpha-beta, boxes for its final α and β and a mark on every branch to click and cross out), or a box
under every state of a graph for its expansion number(s) and a line for the path. "Check my work" grades every box by
code: green right, red wrong (with what belongs there under it), orange left out, plus the first step that went
wrong; it hears the verdict and can walk through from there. "Print blank" and "Print with my answers" give a paper
copy. The page comes from a server on this PC only (127.0.0.1), with a random token in every address. Problems are
exam-sized unless asked for a big one (`size: big`, like the 27-leaf activity sheets). Any kind with a `worksheet`
and `mark` gets one.
## Simulations with sliders

`simulate` opens a panel with a picture and sliders you drag to see what a setting changes. It runs on the practice
problem you're on when it fits (your own graph or tree), else on one made to show the effect well:

- `astar_weight`: f = g + w × h with w from 0 (uniform-cost search) through 1 (the course's A*) up to 5 (nearly
  greedy): fewer states expanded, and past some weight the path stops being the cheapest.
- `search_algorithms`: one graph under BFS, DFS, uniform-cost, greedy and A* (the last two only with a heuristic).
- `alphabeta_order`: the move order (as drawn, best first, worst first) and, on a made-up tree, its size: the
  pruned branches and the count of leaves looked at change, the root's value never does.

Every slider position is worked out by code (the same course rules) before the panel opens, so a drag is instant and
never waits on it; it's told what every position gives, so it can talk about it without seeing the sliders. The
panel shows the frames as images (`avatar/renderer/panel/sim.js`) in one box sized for the tallest, so it doesn't
change height while you drag.

A simulation is a `Simulation` (`src/octavia/study/sims/base.py`): its sliders (`Param`s), a `Frame` (drawing, caption,
up to four numbers) for each mix of positions, and optionally the practice problem kinds it can run on. A subject is a
module that `register`s its simulations, listed in `SUBJECTS` (`sims/__init__.py`); the tool doesn't change. At most
200 frames (and about 1.5 MB) per simulation.

## Concept maps

`concept_map` shows a course's topics as boxes coloured by how well you know each, with arrows for what builds on
what, on a pop-up panel. It grows from the tracker: every exam's topics, plus the topic of any missed question that
isn't one of them. The colours:

- **weak spot** (red, with a ring): marked weak, or two or more misses on questions about it still waiting for review
- **shaky** (amber): a missed question about it still waiting
- **solid** (green): done, nothing waiting
- **learning** (blue): started
- **not started** (grey)

The arrows are links it records (`concept_map` with `links`, from the basis to what builds on it) when it records a
course's topics or explains how two connect; they're kept in the tracker with the course. The map reads from the
basics at the top down to what builds on them; topics with no links yet sit in a block of their own at the bottom.

