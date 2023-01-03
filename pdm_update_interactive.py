from __future__ import annotations

import argparse
import ctypes
import inspect
from collections.abc import Iterable
from itertools import chain

from pdm.cli.actions import do_update
from pdm.cli.commands.update import Command as BaseCommand
from pdm.cli.hooks import HookManager
from pdm.cli.utils import fetch_hashes
from pdm.core import Core
from pdm.models.requirements import Requirement
from pdm.project.core import Project
from pdm.resolver.core import resolve
from pdm.signals import pre_lock
from resolvelib import Resolver
from simple_term_menu import TerminalMenu


def prompt(
    options: Iterable[str],
    title: str | Iterable[str] | None = None,
) -> list[str]:
    terminal_menu = TerminalMenu(options, title=title, multi_select=True)
    menu_entry = terminal_menu.show()
    return [v for i, v in enumerate(options) if i in menu_entry] if menu_entry else []


class InteractiveHookManager(HookManager):
    pass


def pre_lock_signal(
    project: Project,
    requirements: list[Requirement],
    dry_run: bool,
    hooks: HookManager,
) -> None:
    if not isinstance(hooks, InteractiveHookManager):
        return
    lock_frame, update_frame = None, None
    for f in inspect.stack():
        if f.function == "do_lock":
            lock_frame = f
        if f.function == "do_update":
            update_frame = f
        if lock_frame and update_frame:
            break
    else:
        return

    spinner = lock_frame.frame.f_locals["spin"]
    provider = lock_frame.frame.f_locals["provider"]
    with spinner:
        reporter = project.get_reporter(
            requirements,
            provider.tracked_names,
            lock_frame.frame.f_locals["spin"],
        )
        resolver: Resolver = project.core.resolver_class(provider, reporter)
        mapping, dependencies = resolve(
            resolver,
            requirements,
            project.environment.python_requires,
            lock_frame.frame.f_locals["resolve_max_rounds"],
        )
        fetch_hashes(provider.repository, mapping)
    current_candidates = project.locked_repository.all_candidates
    project_dependencies = list(
        chain.from_iterable(
            project.get_dependencies(group)
            for group in update_frame.frame.f_locals["groups"]
        ),
    )
    deps_to_update = [
        f"{name} {c.version} -> {mapping[name].version}"
        for name, c in current_candidates.items()
        if c.version != mapping[name].version and name in project_dependencies
    ]
    prompt_deps = (
        prompt(deps_to_update, "Choose dependencies to update...")
        if deps_to_update
        else []
    )
    prompt_deps = [d.split()[0] for d in prompt_deps]
    tracked_dependencies = [
        n for n, v in current_candidates.items() if n in prompt_deps
    ]

    requires = set()
    collected_dependencies = {}
    for k, v in dependencies.items():
        if k[0] in tracked_dependencies:
            collected_dependencies[k] = v
            requires.update([r.name for r in v])
        elif k[0] in requires:
            collected_dependencies[k] = v
        else:
            collected_dependencies[current_candidates[k[0]].dep_key] = v

    mapping = {
        k: v
        if v.name in tracked_dependencies or v.name in requires
        else current_candidates[k]
        for k, v in mapping.items()
    }

    lock_frame.frame.f_locals.update(
        {
            "mapping": mapping,
            "dependencies": collected_dependencies,
            "resolver": resolver,
            "reporter": reporter,
            "provider": provider,
        },
    )
    lock_frame.frame.f_globals.update(
        {
            "resolve": lambda *args: (mapping, collected_dependencies),
            "fetch_hashes": lambda *args: None,
        },
    )
    ctypes.pythonapi.PyFrame_LocalsToFast(
        ctypes.py_object(lock_frame.frame),
        ctypes.c_int(0),
    )


class Command(BaseCommand):
    def handle(self, project: Project, options: argparse.Namespace) -> None:
        project_groups = (
            list(project.iter_groups())
            if options.default
            else [p for p in project.iter_groups() if p != "default"]
        )
        selected_groups = prompt(project_groups, "Choose dependency groups...")
        if not selected_groups:
            return

        do_update(
            project,
            dev=options.dev,
            groups=selected_groups,
            default=options.default,
            save=options.save_strategy or project.config["strategy.save"],
            strategy=options.update_strategy or project.config["strategy.update"],
            unconstrained=options.unconstrained,
            top=options.top,
            dry_run=options.dry_run,
            packages=options.packages,
            sync=options.sync,
            no_editable=options.no_editable,
            no_self=options.no_self,
            prerelease=options.prerelease,
            hooks=InteractiveHookManager(project, options.skip),
        )


def update_interactive(core: Core) -> None:
    pre_lock.connect(pre_lock_signal)
    core.register_command(Command, "update-interactive")
