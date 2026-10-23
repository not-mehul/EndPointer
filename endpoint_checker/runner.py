"""Run scenarios and read checks, then judge every endpoint that was called."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from endpoint_checker import templating
from endpoint_checker.catalog import Catalog, Endpoint, Scenario, Step
from endpoint_checker.http import Exchange, Skip, Transport
from endpoint_checker.judge import (
    CHANGED,
    NOT_EXERCISED,
    OK,
    SEVERITY,
    Verdict,
    combine,
    judge,
)

AUTO_READS = "Other read endpoints"


@dataclass
class StepOutcome:
    scenario: str
    step: str
    state: str  # "ok" | "failed" | "skipped" | "cleanup" | "leftover"
    detail: str = ""
    endpoint: str | None = None


@dataclass
class EndpointResult:
    id: str
    method: str
    where: str
    effect: str
    exercised: bool = False
    verdict: Verdict | None = None
    calls: int = 0
    statuses: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    observed: list[Any] = field(default_factory=list)  # successful JSON bodies
    reason_not_exercised: str = ""

    @property
    def status(self) -> str:
        return self.verdict.status if self.verdict else NOT_EXERCISED

    @property
    def last_response(self) -> Any:
        return self.observed[-1] if self.observed else None


@dataclass
class CheckResult:
    catalog: str
    org: str
    mode: str
    scenarios: list[str]
    started: str
    finished: str = ""
    steps: list[StepOutcome] = field(default_factory=list)
    endpoints: dict[str, EndpointResult] = field(default_factory=dict)
    leftovers: list[str] = field(default_factory=list)
    unknown: list[Exchange] = field(default_factory=list)
    interrupted: bool = False

    def by_status(self, status: str) -> list[EndpointResult]:
        return sorted((e for e in self.endpoints.values() if e.status == status), key=lambda e: e.id)


# ----------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------


def evaluate(
    catalog: Catalog,
    exchanges: Iterable[Exchange],
    *,
    notes: dict[str, list[tuple[str, str]]] | None = None,
) -> tuple[dict[str, EndpointResult], list[Exchange]]:
    """Per-endpoint results. Exchanges without an endpoint id are matched by URL."""
    from endpoint_checker.matching import Matcher

    results = {
        e.id: EndpointResult(e.id, e.method, e.where, e.effect) for e in catalog.endpoints.values()
    }
    verdicts: dict[str, list[Verdict]] = defaultdict(list)
    unknown: list[Exchange] = []
    matcher = None
    for ex in exchanges:
        ids = [ex.endpoint_id] if ex.endpoint_id in results else []
        if not ids:
            matcher = matcher or Matcher(catalog)
            ids = matcher.match(ex.method, ex.url)
        if not ids:
            unknown.append(ex)
            continue
        for endpoint_id in ids:
            result = results[endpoint_id]
            result.exercised = True
            result.calls += 1
            result.statuses.append(ex.status)
            if ex.ok and ex.response_json is not None:
                result.observed.append(ex.response_json)
            verdicts[endpoint_id].append(judge(ex, catalog.endpoints[endpoint_id]))
    for endpoint_id, items in verdicts.items():
        results[endpoint_id].verdict = combine(items)
    for endpoint_id, entries in (notes or {}).items():
        result = results.get(endpoint_id)
        if result is None:
            continue
        for status, text in entries:
            result.notes.append(text)
            current = result.verdict.status if result.verdict else OK
            if SEVERITY[status] > SEVERITY[current]:
                result.verdict = Verdict(status, text, result.verdict.diffs if result.verdict else [])
            result.exercised = True
    return results, unknown


# ----------------------------------------------------------------------
# Live run
# ----------------------------------------------------------------------


class Run:
    """One check: scenarios, read checks, cleanups, and the evidence they leave."""

    def __init__(
        self,
        catalog: Catalog,
        transport: Transport,
        *,
        inputs: dict | None = None,
        use_setups: bool = True,
        on_step: Callable[[StepOutcome], None] = lambda s: None,
    ):
        self.catalog = catalog
        self.transport = transport
        self.inputs = dict(inputs or {})
        self.use_setups = use_setups
        self.on_step = on_step
        self.notes: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.skipped: dict[str, str] = {}
        self.steps: list[StepOutcome] = []
        self.leftovers: list[str] = []
        self.read_scope: dict = {}
        self.interrupted = False
        self._run_cleanups: list[tuple[str, Step, dict]] = []
        self._setup_state: dict[str, str | None] = {}
        self._mark = len(transport.exchanges)
        transport.before_send = self._ensure_api

    # -- outcomes -------------------------------------------------------

    def _emit(self, outcome: StepOutcome) -> None:
        self.steps.append(outcome)
        self.on_step(outcome)

    # -- API setup (e.g. a temporary public-API key) ---------------------

    def _ensure_api(self, endpoint: Endpoint) -> None:
        if endpoint.auth == "none":
            return
        setup_id = (self.catalog.apis.get(endpoint.api) or {}).get("setup")
        if not setup_id:
            return
        if endpoint.api in self._setup_state:
            error = self._setup_state[endpoint.api]
            if error:
                raise Skip(f"{endpoint.api} API unavailable ({error})")
            return
        if not self.use_setups:
            self._setup_state[endpoint.api] = "skipped for this run"
            raise Skip(f"{endpoint.api} API checks skipped for this run")
        self._setup_state[endpoint.api] = None  # in progress
        scenario = self.catalog.scenarios[setup_id]
        ok = self.run_scenario(scenario, scope=self.transport.vars, setup=True)
        if not ok:
            self._setup_state[endpoint.api] = f"setup '{scenario.title}' failed"
            raise Skip(f"{endpoint.api} API unavailable (setup '{scenario.title}' failed)")

    # -- scenarios --------------------------------------------------------

    def run_scenario(self, scenario: Scenario, *, scope: dict | None = None, setup: bool = False) -> bool:
        """Run every step (failures don't stop later steps), then the cleanups."""
        local = scope if scope is not None else {}
        missing = [name for name in scenario.inputs if not self.inputs.get(name)]
        if missing:
            self._emit(StepOutcome(scenario.title, "(all steps)", "skipped", f"needs input {', '.join(missing)}"))
            for endpoint_id in scenario.calls():
                self.skipped.setdefault(endpoint_id, f"scenario '{scenario.title}' needs input {', '.join(missing)}")
            return False
        for name in scenario.inputs:
            local[name] = self.inputs[name]
        for name, template in scenario.vars.items():
            try:
                local[name] = templating.render(template, self.transport.scope(local), stamp=self.transport.stamp)
            except templating.Unresolved as e:
                self._emit(StepOutcome(scenario.title, f"variable {name}", "skipped", str(e)))
                return False
        cleanups: list[tuple[str, Step, dict]] = []
        all_ok = True
        try:
            for step in scenario.steps:
                called, clean = self._step(scenario.title, step, local)
                all_ok = all_ok and clean
                if called and step.cleanup:
                    target = self._run_cleanups if setup else cleanups
                    for cleanup in step.cleanup:
                        target.append((scenario.title, cleanup, dict(local)))
        finally:
            self._cleanup(cleanups)
        return all_ok

    def _step(self, title: str, step: Step, local: dict, *, cleanup: bool = False) -> tuple[bool, bool]:
        """Run one step → (call succeeded, everything checked out)."""
        endpoint = self.catalog.endpoint(step.call)
        ok_state, bad_state = ("cleanup", "leftover") if cleanup else ("ok", "failed")
        try:
            extra = templating.render(step.with_, self.transport.scope(local), stamp=self.transport.stamp)
            exchange = self.transport.send(endpoint, {**local, **extra})
        except (Skip, templating.Unresolved) as e:
            self.skipped.setdefault(endpoint.id, str(e))
            self._emit(StepOutcome(title, step.name, bad_state if cleanup else "skipped", str(e), endpoint.id))
            return False, False
        if exchange.status == 0:
            self._emit(StepOutcome(title, step.name, bad_state, f"no response: {exchange.error}", endpoint.id))
            return False, False
        if not endpoint.status_ok(exchange.status):
            detail = f"HTTP {exchange.status}" + (f": {exchange.message()}" if exchange.message() else "")
            self._emit(StepOutcome(title, step.name, bad_state, detail, endpoint.id))
            return False, False

        problems: list[str] = []
        scope = self.transport.scope(local)
        for var, path in step.save.items():
            try:
                path = templating.render(path, scope, stamp=self.transport.stamp)
                value = templating.extract(exchange.raw_json, path)
            except (templating.PathError, templating.Unresolved) as e:
                problems.append(str(e))
                continue
            if value is not None:
                local[var] = value
        scope = self.transport.scope(local)
        problems += self._expectations(step, exchange, scope)
        for problem in problems:
            self.notes[endpoint.id].append((CHANGED, f"{step.name}: {problem}"))
        state = ok_state if not problems else bad_state
        self._emit(StepOutcome(title, step.name, state, "; ".join(problems), endpoint.id))
        return True, not problems

    def _expectations(self, step: Step, exchange: Exchange, scope: dict) -> list[str]:
        problems = []
        body = exchange.raw_json
        stamp = self.transport.stamp
        try:
            for path in step.expect.get("exists", []):
                if not templating.query(body, templating.render(path, scope, stamp=stamp)):
                    problems.append(f"response has nothing at {path}")
            for path, wanted in (step.expect.get("contains") or {}).items():
                values = templating.query(body, templating.render(path, scope, stamp=stamp))
                wanted = templating.render(wanted, scope, stamp=stamp)
                if str(wanted) not in {str(v) for v in values}:
                    problems.append(f"expected {wanted!r} among {path} ({len(values)} value(s) returned)")
            for path, wanted in (step.expect.get("equals") or {}).items():
                values = templating.query(body, templating.render(path, scope, stamp=stamp))
                wanted = templating.render(wanted, scope, stamp=stamp)
                if not values or values[0] != wanted:
                    problems.append(f"expected {path} == {wanted!r}, got {values[0] if values else 'nothing'!r}")
        except templating.Unresolved as e:
            problems.append(f"couldn't check: {e}")
        return problems

    # -- cleanup ----------------------------------------------------------

    def _cleanup(self, cleanups: list[tuple[str, Step, dict]]) -> None:
        """Undo in reverse order; a chain stops at its first failure."""
        while cleanups:
            title, step, scope = cleanups.pop()
            failed = self._cleanup_step(title, step, scope)
            if failed:
                self.leftovers.append(f"{title}: {failed}")

    def _cleanup_step(self, title: str, step: Step, scope: dict) -> str:
        """Run a cleanup chain; returns "" or what couldn't be cleaned up."""
        called, clean = self._step(title, step, scope, cleanup=True)
        if not (called and clean):
            if step.otherwise is not None:
                fallback, _ = self._step(title, step.otherwise, scope, cleanup=True)
                if fallback:
                    return f"{step.name} failed; did '{step.otherwise.name}' instead"
            return f"{step.name} failed"
        for nested in step.cleanup:  # follow-up steps of a cleanup chain
            failed = self._cleanup_step(title, nested, scope)
            if failed:
                return failed
        return ""

    # -- automatic read checks ------------------------------------------

    def auto_reads(self) -> None:
        """Call every remaining read endpoint whose variables can be filled."""
        exercised = {ex.endpoint_id for ex in self.transport.exchanges[self._mark :]}
        setup_calls = {
            c
            for api in self.catalog.apis.values()
            if api.get("setup") in self.catalog.scenarios
            for c in self.catalog.scenarios[api["setup"]].calls()
        }
        for endpoint in sorted(self.catalog.endpoints.values(), key=lambda e: e.id):
            if (
                not endpoint.is_read
                or endpoint.autocheck is False
                or endpoint.auth == "none"
                or endpoint.id in exercised
                or endpoint.id in setup_calls
            ):
                continue
            try:
                exchange = self.transport.send(endpoint, self.read_scope)
            except (Skip, templating.Unresolved) as e:
                self.skipped.setdefault(endpoint.id, str(e))
                continue
            if exchange.status == 0 or not endpoint.status_ok(exchange.status):
                detail = f"HTTP {exchange.status}" if exchange.status else f"no response: {exchange.error}"
                self._emit(StepOutcome(AUTO_READS, endpoint.id, "failed", detail, endpoint.id))
            else:
                self._emit(StepOutcome(AUTO_READS, endpoint.id, "ok", "", endpoint.id))

    # -- the whole check --------------------------------------------------

    def check(self, scenarios: list[Scenario], *, auto_reads: bool = True) -> CheckResult:
        """Read scenarios, then automatic reads, then the chosen write scenarios."""
        reads = self.catalog.scenarios_of_kind("read")
        writes = [s for s in scenarios if s.kind in ("write", "settings")]
        result = CheckResult(
            catalog=self.catalog.name,
            org=str(self.transport.vars.get("org_short_name", "")),
            mode="write" if writes else "read-only",
            scenarios=[s.title for s in reads + writes],
            started=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        try:
            for scenario in reads:
                self.run_scenario(scenario, scope=self.read_scope)
            if auto_reads:
                self.auto_reads()
            for scenario in writes:
                self.run_scenario(scenario, scope={})
        except KeyboardInterrupt:
            self.interrupted = True
        finally:
            self.finish()
        exchanges = self.transport.exchanges[self._mark :]
        result.endpoints, result.unknown = evaluate(self.catalog, exchanges, notes=self.notes)
        self._explain(result, reads + writes)
        result.steps = self.steps
        result.leftovers = self.leftovers
        result.interrupted = self.interrupted
        result.finished = time.strftime("%Y-%m-%d %H:%M:%S")
        return result

    def finish(self) -> None:
        """Run-level cleanups (e.g. delete the temporary API key) and forget what setups provided."""
        self._cleanup(self._run_cleanups)
        for api_name in self._setup_state:
            scenario = self.catalog.scenarios.get(self.catalog.apis[api_name].get("setup", ""))
            for step in scenario.steps if scenario else []:
                for var in step.save:
                    self.transport.vars.pop(var, None)
            for var in scenario.vars if scenario else []:
                self.transport.vars.pop(var, None)
        self._setup_state.clear()
        self.transport.before_send = lambda endpoint: None

    def _explain(self, result: CheckResult, ran: list[Scenario]) -> None:
        ran_ids = {s.id for s in ran}
        for endpoint_id, item in result.endpoints.items():
            if item.exercised:
                continue
            endpoint = self.catalog.endpoints[endpoint_id]
            if endpoint_id in self.skipped:
                item.reason_not_exercised = self.skipped[endpoint_id]
                continue
            others = [s for s in self.catalog.scenarios_calling(endpoint_id) if s.id not in ran_ids]
            if any(s.id in ran_ids for s in self.catalog.scenarios_calling(endpoint_id)):
                item.reason_not_exercised = "not reached in this run"
            elif others:
                item.reason_not_exercised = "in scenario " + ", ".join(f"'{s.title}'" for s in others) + " (not run)"
            elif endpoint.auth == "none":
                item.reason_not_exercised = "used during login only"
            elif endpoint.is_read and endpoint.autocheck is False:
                item.reason_not_exercised = "autocheck is off" + (f" — {endpoint.notes}" if endpoint.notes else "")
            elif not endpoint.is_read:
                item.reason_not_exercised = f"{endpoint.effect} endpoint in no scenario — try it with `call {endpoint_id}`"
            else:
                item.reason_not_exercised = "not exercised"


def analyze(catalog: Catalog, exchanges: list[Exchange], *, source: str) -> CheckResult:
    """Judge recorded traffic (a browser HAR) without logging in."""
    result = CheckResult(
        catalog=catalog.name,
        org="",
        mode=f"recorded traffic: {source}",
        scenarios=[],
        started=exchanges[0].ts if exchanges else "",
        finished=exchanges[-1].ts if exchanges else "",
    )
    result.endpoints, result.unknown = evaluate(catalog, exchanges)
    for item in result.endpoints.values():
        if not item.exercised:
            item.reason_not_exercised = "not called in this recording"
    return result
