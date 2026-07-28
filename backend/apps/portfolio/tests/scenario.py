"""A minimal, deterministic portfolio for the consumer tests to react to.

Built in Python rather than loaded from the seed fixtures on purpose. The fixtures are the
operation's real data and they change as the dataset does; a consumer test asserting "the score
moved" against them would fail the day someone reprices a project, for a reason that has nothing
to do with the consumer. What is built here is the smallest graph the engine can score: one
client, one owner, one workflow with the three categories the specifications branch on, one
engagement type, one priority and one active policy.

Not a factory library. Everything is created explicitly and with fixed codes, because a test that
reads ``PRJ-T1`` in an assertion should be able to find where ``PRJ-T1`` was made.
"""

from datetime import date

from apps.accounts.models import User
from apps.accounts.tests.support import make_member
from apps.catalog.models import Currency, EngagementType, Priority
from apps.portfolio.models import Client, Project
from apps.prioritization.models import PriorityPolicy
from apps.work.models import Task
from apps.workflow.models import (
    AppliesTo,
    StateCategory,
    Workflow,
    WorkflowState,
    WorkflowTransition,
)

#: The weights of the seed policy (ARCHITECTURE §4.1). Repeated here rather than imported from the
#: fixture so the tests keep scoring against a known criterion when the operation retunes the real
#: one — a rebalanced portfolio must not turn a consumer test red.
POLICY_WEIGHTS = {
    "deadline_pressure": 0.25,
    "overdue_work": 0.20,
    "criticality": 0.15,
    "business_value": 0.15,
    "blockage": 0.15,
    "staleness": 0.10,
}

OWNER_CODE = "tester"
#: The ops lead, created only by :meth:`PortfolioScenario.add_ops_lead`. Opt-in because a second
#: person on the roster changes what ``GET /team/load`` returns, and a load test should not have to
#: know that an authorization test exists.
OPS_LEAD_CODE = "tester.lead"
PROJECT_CODE = "PRJ-T1"
SECOND_PROJECT_CODE = "PRJ-T2"


class PortfolioScenario:
    """The rows one consumer test needs, created once and addressed by attribute."""

    def __init__(self) -> None:
        """Create the whole graph. Cheap enough to run per test, which keeps tests independent."""
        self.policy = PriorityPolicy.objects.create(
            version="test-v1",
            is_active=True,
            weights=POLICY_WEIGHTS,
            modifiers={"engagement_type": True},
        )
        self.workflow = Workflow.objects.create(
            code="test-project-flow",
            name="Test project flow",
            applies_to=AppliesTo.PROJECT,
            is_default=True,
        )
        self.backlog = WorkflowState.objects.create(
            workflow=self.workflow,
            code="discovery",
            label="Descubrimiento",
            category=StateCategory.BACKLOG,
            is_initial=True,
            order=1,
        )
        self.in_progress = WorkflowState.objects.create(
            workflow=self.workflow,
            code="execution",
            label="Ejecución",
            category=StateCategory.IN_PROGRESS,
            order=2,
        )
        self.blocked = WorkflowState.objects.create(
            workflow=self.workflow,
            code="blocked",
            label="Bloqueado",
            category=StateCategory.BLOCKED,
            order=3,
        )
        self.engagement_type = EngagementType.objects.create(
            code="proyecto", label="Proyecto", weight="1.10"
        )
        self.currency = Currency.objects.create(
            code="USD", label="Dolar estadounidense", minor_units=2
        )
        self.priority = Priority.objects.create(
            code="critica", label="Crítica", is_urgent=True, weight="1.50"
        )
        self.client = Client.objects.create(code="atlas", alias="Atlas Foods")
        # Built through the identity context's own helper, so the owner can actually sign in:
        # every route is authenticated now, and a scenario whose people had no password would only
        # be able to exercise 401s.
        self.owner = make_member(code=OWNER_CODE)
        self.owner.alias = "Test Owner"
        self.owner.weekly_capacity_points = 10
        self.owner.save(update_fields=["alias", "weekly_capacity_points"])
        self.project = self._project(PROJECT_CODE, "Primary test project")
        self.other_project = self._project(SECOND_PROJECT_CODE, "Second project, same owner")

    def add_ops_lead(self) -> User:
        """Add the one person allowed to override the ranking and rebuild the whole portfolio.

        Opt-in rather than built in ``__init__`` because they own no projects and carry no tasks,
        and a roster that grows silently would change every ``GET /team/load`` assertion in the
        suite for a reason that has nothing to do with load.

        Returns:
            The ops lead, ready to sign in.
        """
        return make_member(code=OPS_LEAD_CODE, is_ops_lead=True)

    def _project(self, code: str, name: str) -> Project:
        """One scored-but-unremarkable project: dated, owned, with a next step recorded."""
        return Project.objects.create(
            code=code,
            name=name,
            client=self.client,
            engagement_type=self.engagement_type,
            workflow_state=self.in_progress,
            owner=self.owner,
            target_date=date(2026, 12, 31),
            business_value="10000.00",
            currency=self.currency,
            next_step="Keep going",
        )

    def add_task(self, *, code: str, due_date: date | None = None) -> Task:
        """Attach one open, urgent task to the primary project.

        Urgent and open because those are the two counts the ``criticality`` and ``overdue_work``
        signals read, so a single task is enough to move a score measurably.
        """
        return Task.objects.create(
            code=code,
            project=self.project,
            assignee=self.owner,
            priority=self.priority,
            workflow_state=self.in_progress,
            due_date=due_date,
            title=f"Task {code}",
        )

    def add_transitions(self) -> None:
        """Declare the two project edges the HTTP tests exercise.

        Opt-in rather than built in ``__init__``, so a consumer test that only scores keeps
        creating the smallest graph that can be scored. ``execution -> blocked`` requires a reason
        and ``discovery -> execution`` does not, which is exactly the pair needed to prove that
        the requirement is data and not code — and that its absence is a typed 422 rather than a
        silent move.

        Note what is *not* declared: no edge leaves ``blocked``. A project moved there is stuck by
        construction, which is what makes "this move is illegal" testable without inventing a
        state nobody would configure.
        """
        WorkflowTransition.objects.create(
            workflow=self.workflow,
            from_state=self.in_progress,
            to_state=self.blocked,
            label="Marcar como bloqueado",
            requires_reason=True,
            order=1,
        )
        WorkflowTransition.objects.create(
            workflow=self.workflow,
            from_state=self.backlog,
            to_state=self.in_progress,
            label="Arrancar ejecución",
            order=1,
        )

    def add_task_workflow(self) -> WorkflowState:
        """Declare the task workflow, so tasks can be created and moved.

        Separate from the project workflow because ``Workflow.applies_to`` is what
        ``Workflow.objects.resolve`` keys on: a task created while only a project workflow exists
        raises ``WorkflowNotConfigured``, which is correct and is why this is explicit.

        Returns:
            The task workflow's initial state.
        """
        task_workflow = Workflow.objects.create(
            code="test-task-flow",
            name="Test task flow",
            applies_to=AppliesTo.TASK,
            is_default=True,
        )
        todo = WorkflowState.objects.create(
            workflow=task_workflow,
            code="todo",
            label="Por hacer",
            category=StateCategory.BACKLOG,
            is_initial=True,
            order=1,
        )
        doing = WorkflowState.objects.create(
            workflow=task_workflow,
            code="doing",
            label="En progreso",
            category=StateCategory.IN_PROGRESS,
            order=2,
        )
        WorkflowTransition.objects.create(
            workflow=task_workflow,
            from_state=todo,
            to_state=doing,
            label="Empezar",
            order=1,
        )
        return todo

    def block_project(self) -> None:
        """Move the primary project into a ``BLOCKED`` state, the way a transition would.

        Assigned directly because this is a fixture, not a use case: the transition service is
        what a request goes through, and exercising it here would test the workflow context inside
        a consumer test.
        """
        self.project.workflow_state = self.blocked
        self.project.save(update_fields=["workflow_state", "updated_at"])
