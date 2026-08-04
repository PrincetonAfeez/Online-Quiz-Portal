"""Gradebook and item analysis.

Everything here reads finalized attempts only. In-progress work is not a grade
and must never appear in a distribution or a discrimination index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from assessments.models import Attempt, AttemptItemSnapshot, Response


@dataclass
class GradebookRow:
    student_id: int
    student_name: str
    username: str
    attempts: int
    best_score: Decimal
    best_percent: Decimal
    latest_status: str
    is_late: bool
    finalized_at: object
    passed: bool | None


@dataclass
class ItemStat:
    """Classical test theory statistics for one question.

    difficulty (p-value): fraction answering correctly. Very high or very low
    values carry little information about who knows the material.

    discrimination (point-biserial-like): how much better the top scorers did on
    this item than the bottom scorers. Near zero or negative means the item is
    not measuring the same thing as the rest of the paper -- usually a wording
    bug or a wrong answer key.
    """

    prompt: str
    responses: int
    correct: int
    difficulty: Decimal
    discrimination: Decimal
    flags: list[str] = field(default_factory=list)

    @property
    def difficulty_label(self) -> str:
        if self.difficulty >= Decimal("0.9"):
            return "Very easy"
        if self.difficulty >= Decimal("0.7"):
            return "Easy"
        if self.difficulty >= Decimal("0.4"):
            return "Moderate"
        if self.difficulty >= Decimal("0.2"):
            return "Hard"
        return "Very hard"


@dataclass
class AssignmentAnalytics:
    assignment_title: str
    finalized_attempts: int
    students_attempted: int
    mean_percent: Decimal
    median_percent: Decimal
    pass_rate: Decimal | None
    distribution: list[tuple[str, int]]
    items: list[ItemStat]
    late_count: int


def _quantize(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.1"))


def scored_attempts(assignment):
    return (
        Attempt.objects.filter(
            assignment=assignment, status__in=Attempt.SCORED_STATUSES
        )
        .select_related("student")
        .order_by("student_id", "-score")
    )


def gradebook_rows(assignment, pass_mark: int | None = None) -> list[GradebookRow]:
    """One row per student, keyed on their best scored attempt."""
    by_student: dict[int, list[Attempt]] = {}
    for attempt in scored_attempts(assignment):
        by_student.setdefault(attempt.student_id, []).append(attempt)

    rows = []
    for student_id, attempts in by_student.items():
        best = max(attempts, key=lambda a: (a.score, a.finalized_at or a.started_at))
        latest = max(attempts, key=lambda a: a.started_at)
        percent = best.score_percent
        rows.append(
            GradebookRow(
                student_id=student_id,
                student_name=best.student.get_full_name() or best.student.username,
                username=best.student.username,
                attempts=len(attempts),
                best_score=best.score,
                best_percent=percent,
                latest_status=latest.status,
                is_late=any(a.is_late for a in attempts),
                finalized_at=best.finalized_at,
                passed=None if pass_mark is None else percent >= pass_mark,
            )
        )
    rows.sort(key=lambda row: row.student_name.lower())
    return rows


def _median(values: list[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _distribution(percents: list[Decimal]) -> list[tuple[str, int]]:
    buckets = [
        ("0-19", 0), ("20-39", 0), ("40-59", 0), ("60-79", 0), ("80-100", 0),
    ]
    counts = dict(buckets)
    for percent in percents:
        if percent < 20:
            counts["0-19"] += 1
        elif percent < 40:
            counts["20-39"] += 1
        elif percent < 60:
            counts["40-59"] += 1
        elif percent < 80:
            counts["60-79"] += 1
        else:
            counts["80-100"] += 1
    return [(label, counts[label]) for label, _ in buckets]


def item_statistics(assignment) -> list[ItemStat]:
    """Per-question difficulty and discrimination across finalized attempts.

    Items are grouped by prompt text, so sampled forms -- where each attempt has
    its own snapshot rows for the same underlying question -- still aggregate.
    """
    attempts = list(scored_attempts(assignment))
    if not attempts:
        return []

    attempt_ids = [a.id for a in attempts]
    percent_by_attempt = {a.id: a.score_percent for a in attempts}

    snapshots = list(
        AttemptItemSnapshot.objects.filter(attempt_id__in=attempt_ids).prefetch_related(
            "choice_snapshots"
        )
    )
    correct_by_snapshot = {
        snapshot.id: {c.id for c in snapshot.choice_snapshots.all() if c.is_correct}
        for snapshot in snapshots
    }
    selected_by_snapshot: dict[int, set[int]] = {}
    for response in Response.objects.filter(attempt_id__in=attempt_ids).prefetch_related(
        "selected_choices"
    ):
        selected_by_snapshot[response.item_snapshot_id] = {
            c.id for c in response.selected_choices.all()
        }

    # Rank attempts to form top and bottom groups for discrimination.
    ranked = sorted(attempt_ids, key=lambda aid: percent_by_attempt[aid], reverse=True)
    group_size = max(1, len(ranked) // 3)
    top_group = set(ranked[:group_size])
    bottom_group = set(ranked[-group_size:])

    grouped: dict[str, list] = {}
    for snapshot in snapshots:
        grouped.setdefault(snapshot.prompt, []).append(snapshot)

    stats = []
    for prompt, rows in grouped.items():
        total = len(rows)
        correct = 0
        top_correct = top_total = bottom_correct = bottom_total = 0
        for snapshot in rows:
            answer = selected_by_snapshot.get(snapshot.id, set())
            key = correct_by_snapshot.get(snapshot.id, set())
            is_correct = bool(key) and answer == key
            correct += 1 if is_correct else 0
            if snapshot.attempt_id in top_group:
                top_total += 1
                top_correct += 1 if is_correct else 0
            if snapshot.attempt_id in bottom_group:
                bottom_total += 1
                bottom_correct += 1 if is_correct else 0

        difficulty = Decimal(correct) / Decimal(total) if total else Decimal("0")
        top_rate = Decimal(top_correct) / Decimal(top_total) if top_total else Decimal("0")
        bottom_rate = (
            Decimal(bottom_correct) / Decimal(bottom_total) if bottom_total else Decimal("0")
        )
        discrimination = top_rate - bottom_rate

        flags = []
        if total >= 5:
            if discrimination <= Decimal("0"):
                flags.append("Negative discrimination — check the answer key")
            elif discrimination < Decimal("0.15"):
                flags.append("Low discrimination — wording may be ambiguous")
            if difficulty >= Decimal("0.95"):
                flags.append("Nearly everyone correct — little information")
            if difficulty <= Decimal("0.15"):
                flags.append("Nearly everyone wrong — check the key or teaching")

        stats.append(
            ItemStat(
                prompt=prompt,
                responses=total,
                correct=correct,
                difficulty=difficulty.quantize(Decimal("0.01")),
                discrimination=discrimination.quantize(Decimal("0.01")),
                flags=flags,
            )
        )
    stats.sort(key=lambda s: s.discrimination)
    return stats


def assignment_analytics(assignment, pass_mark: int | None = None) -> AssignmentAnalytics:
    attempts = list(scored_attempts(assignment))
    percents = [a.score_percent for a in attempts]
    students = {a.student_id for a in attempts}

    pass_rate = None
    if pass_mark is not None and percents:
        passed = sum(1 for p in percents if p >= pass_mark)
        pass_rate = _quantize(Decimal(passed) / Decimal(len(percents)) * 100)

    return AssignmentAnalytics(
        assignment_title=assignment.title,
        finalized_attempts=len(attempts),
        students_attempted=len(students),
        mean_percent=_quantize(sum(percents) / len(percents)) if percents else Decimal("0"),
        median_percent=_quantize(_median(percents)),
        pass_rate=pass_rate,
        distribution=_distribution(percents),
        items=item_statistics(assignment),
        late_count=sum(1 for a in attempts if a.is_late),
    )
