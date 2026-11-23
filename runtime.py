"""Coordinating runtime for Testarossa's isolated intelligence foundation.

The runtime validates plans, discovers registered capabilities, and routes
only explicitly allowlisted Blender read operations to ReadOnlyToolExecutor.

It does not execute arbitrary tools, run generated code, or perform mutations.
"""
from pathlib import Path
from typing import Any

from .gateway import ToolGateway
from .discovery import ToolDiscovery
from .learning import LearningEngine
from .memory import ExperienceLog, SkillLibrary
from .planner import Plan, validate_plan
from .read_executor import ReadOnlyToolExecutor
from .registry import Registry


class TestarossaRuntime:
    """Safe coordinator around planning, memory, and read-only execution."""

    READ_EXECUTABLE_TOOLS = frozenset(ReadOnlyToolExecutor.ALLOWED)

    def __init__(
        self,
        observer,
        memory_directory,
        registry=None,
        executor=None,
    ):
        if not callable(observer) and executor is None:
            raise ValueError("observer_must_be_callable")

        self.registry = registry or Registry()
        self.discovery = ToolDiscovery(self.registry.tools)
        self.gateway = ToolGateway(tools=self.registry.tools)
        self.executor = executor or ReadOnlyToolExecutor(observer)

        root = Path(memory_directory)
        self.skills = SkillLibrary(root / "skills")
        self.experiences = ExperienceLog(root / "experiences")
        self.learning = LearningEngine()

    def discover_tools(
        self, capability=None, include_mutations=False, query=None, limit=8
    ):
        """Discover registered tools; discovery never grants execution permission."""
        if capability is not None and (
            type(capability) is not str
            or not capability.strip()
            or len(capability) > 100
        ):
            raise ValueError("invalid_capability")
        if type(include_mutations) is not bool:
            raise ValueError("invalid_mutation_filter")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("invalid_limit")

        if query is None:
            tools = self.registry.describe(capability=capability)
            if not include_mutations:
                tools = [
                    item for item in tools
                    if item["risk"] == "read" and not item["approval_required"]
                ]
            return tools

        if type(query) is not str or not query.strip() or len(query) > 500:
            raise ValueError("invalid_query")

        ranked = self.discovery.discover(
            query=query,
            capability=capability,
            include_mutations=include_mutations,
            limit=limit,
        )
        return [
            {
                "name": item.tool.name,
                "description": item.tool.description,
                "arguments": list(item.tool.arguments),
                "capability": item.tool.capability,
                "risk": item.tool.risk,
                "approval_required": item.tool.approval_required,
                "score": item.score,
                "matched_terms": list(item.matched_terms),
            }
            for item in ranked
        ]

    def validate_plan(self, proposal):
        """Validate a model-proposed plan against registered capabilities."""
        capabilities = {
            item["capability"]
            for item in self.registry.describe()
        }
        plan = validate_plan(proposal, capabilities)
        capability_requires_approval = {
            item["capability"]: (
                item["risk"] != "read" or item["approval_required"]
            )
            for item in self.registry.describe()
        }
        for step in plan.steps:
            if (
                capability_requires_approval.get(step.capability, False)
                and not step.requires_user_approval
            ):
                raise ValueError("approval_required_for_mutation_capability")
        return plan

    def execute_read_tool(
        self,
        tool_name,
        arguments,
        goal="Blender inspection",
        stage="read_execution",
    ):
        """Execute one explicitly allowlisted read-only Blender operation."""
        if type(goal) is not str or not goal.strip() or len(goal) > 500:
            raise ValueError("invalid_goal")
        if type(stage) is not str or not stage.strip() or len(stage) > 80:
            raise ValueError("invalid_stage")
        if type(tool_name) is not str:
            raise ValueError("invalid_tool_name")
        if tool_name not in self.READ_EXECUTABLE_TOOLS:
            raise PermissionError("runtime_tool_not_executable")

        tool = self.registry.get(tool_name)
        if tool.risk != "read" or tool.approval_required:
            raise PermissionError("runtime_read_only_boundary")

        prepared = self.gateway.prepare(tool_name, arguments)

        try:
            result = self.executor.execute(prepared)
        except Exception as exc:
            self.experiences.record(
                goal=goal,
                stage=stage,
                outcome="failure",
                summary="Read-only operation failed; no automatic retry was attempted.",
                error_code=type(exc).__name__[:80],
            )
            raise

        self.experiences.record(
            goal=goal,
            stage=stage,
            outcome="success",
            summary="Read-only operation completed; result returned for inspection.",
        )
        return result

    def execute_plan_step(
        self,
        plan,
        step_id,
        tool_name,
        arguments,
    ):
        """Execute a plan step only when its declared capability matches.

        Approval-required steps are deliberately not executed by this runtime.
        A future mutation executor must be separately integrated with the
        existing user approval and broker workflow.
        """
        if not isinstance(plan, Plan):
            raise ValueError("validated_plan_required")
        if type(step_id) is not str or type(tool_name) is not str:
            raise ValueError("invalid_step_selection")

        step = next((item for item in plan.steps if item.id == step_id), None)
        if step is None:
            raise ValueError("plan_step_not_found")

        tool = self.registry.get(tool_name)
        if tool.capability != step.capability:
            raise PermissionError("plan_tool_capability_mismatch")

        if step.requires_user_approval:
            raise PermissionError("plan_step_requires_approval")

        if tool_name not in self.READ_EXECUTABLE_TOOLS:
            raise PermissionError("plan_step_not_read_executable")

        return self.execute_read_tool(
            tool_name,
            arguments,
            goal=plan.goal,
            stage=step.id,
        )

    def record_outcome(
        self,
        goal,
        stage,
        outcome,
        summary,
        error_code=None,
    ):
        """Record a bounded experience; this never triggers an action."""
        return self.experiences.record(
            goal, stage, outcome, summary, error_code
        )

    def recent_experiences(self, limit=20):
        return self.experiences.recent(limit)

    def learning_summary(self, limit=100):
        """Summarize recorded outcomes for controlled learning."""
        records = self.experiences.recent(limit)
        normalized = [
            {
                "outcome": item["outcome"],
                "error": item.get("error_code") or "",
                "operation": item["stage"],
            }
            for item in records
        ]
        return self.learning.analyze_history(normalized)

    def recovery_advice(
        self,
        outcome,
        error="",
        retry_count=0,
        approval_required=False,
    ):
        """Return advice only; never retry or repair automatically."""
        return self.learning.recommend_recovery(
            outcome,
            error=error,
            retry_count=retry_count,
            approval_required=approval_required,
        )

    def search_skills(self, query, limit=10):
        return self.skills.search(query, limit)

    def save_reviewed_skill(
        self,
        name,
        title,
        purpose,
        prerequisites,
        steps,
        verification,
        user_approved,
        tags=(),
    ):
        """Save a skill only after an explicit approval boolean is supplied."""
        if type(user_approved) is not bool:
            raise ValueError("approval_must_be_boolean")
        if not user_approved:
            raise PermissionError("explicit_user_approval_required")
        return self.skills.add(
            name=name,
            title=title,
            purpose=purpose,
            prerequisites=prerequisites,
            steps=steps,
            verification=verification,
            tags=tags,
        )
