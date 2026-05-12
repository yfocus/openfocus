# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import AgentSpace, Goal, Task


class GoalRepository:
    def __init__(self, s: Session):
        self.s = s

    def get(self, goal_id: int) -> Goal | None:
        return self.s.get(Goal, int(goal_id))

    def add(self, goal: Goal) -> Goal:
        self.s.add(goal)
        self.s.flush()
        return goal

    def delete(self, goal: Goal) -> None:
        self.s.delete(goal)


class TaskRepository:
    def __init__(self, s: Session):
        self.s = s

    def get(self, task_id: int) -> Task | None:
        return self.s.get(Task, int(task_id))

    def list_by_goal(self, goal_id: int) -> list[Task]:
        return self.s.query(Task).filter(Task.goal_id == int(goal_id)).all()

    def add(self, task: Task) -> Task:
        self.s.add(task)
        self.s.flush()
        return task

    def delete(self, task: Task) -> None:
        self.s.delete(task)


class AgentSpaceRepository:
    def __init__(self, s: Session):
        self.s = s

    def get_by_task_public_id(self, task_public_id: str) -> AgentSpace | None:
        return (
            self.s.query(AgentSpace)
            .filter(AgentSpace.task_public_id == str(task_public_id or ""))
            .one_or_none()
        )

    def delete(self, space: AgentSpace) -> None:
        self.s.delete(space)
