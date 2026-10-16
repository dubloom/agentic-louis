"""Terminal presentation for the blocked-ticket picker."""

from textwrap import shorten

import questionary
from prompt_toolkit.output import ColorDepth
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text


def blocked_heading(console, count):
    console.print()
    console.print("agentic-louis", style="dim")
    heading = Text("Blocked agents", style="bold")
    heading.append(f"  {count} ticket{'s' if count != 1 else ''}", style="yellow")
    console.print(heading)
    console.print()


def blocked_tickets(console, tickets):
    blocked_heading(console, len(tickets))
    if not tickets:
        console.print("No blocked agent tickets. You're all caught up.", style="green")
        console.print()
        return

    compact = console.width < 64
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False,
                  padding=(0, 1), leading=1, border_style="dim", header_style="dim",
                  width=min(console.width, 110))
    table.add_column("#", style="dim", justify="right", no_wrap=True)
    table.add_column("Ticket / task", ratio=1)
    if not compact:
        table.add_column("Session", no_wrap=True)
    for number, (issue, project, detail, available) in enumerate(tickets, 1):
        # Text keeps Jira titles and diagnostics literal, even if they contain markup.
        task = Text(overflow="ellipsis", no_wrap=True)
        task.append(issue["key"], style="bold cyan")
        task.append(f" [{project}]", style="dim")
        task.append("\n" + " ".join(issue["fields"]["summary"].split()))
        if detail:
            task.append("\n" + " ".join(detail.split()), style="dim" if available else "yellow")
        status = Text("Saved" if available else "Unavailable", style="green" if available else "yellow")
        if compact:
            task.append("\n")
            task.append_text(status)
            table.add_row(str(number), task)
        else:
            table.add_row(str(number), task, status)
    console.print(table)


def selection_prompt(console, tickets):
    blocked_heading(console, len(tickets))
    width = max(24, min(console.width, 110) - 6)
    choices = []
    for issue, project, detail, available in tickets:
        title = f"{issue['key']} [{project}] {' '.join(issue['fields']['summary'].split())}"
        choices.append(questionary.Choice(
            shorten(title, width=width, placeholder="…"), value=issue["key"],
            disabled=None if available else "no saved session",
            description=shorten(" ".join(detail.split()), width=width * 2, placeholder="…") if detail else None,
        ))
    choices.append(questionary.Choice("Cancel", value=""))
    return questionary.select(
        "Resume a ticket", choices=choices, qmark="›", pointer="❯",
        instruction="↑/↓ move · Enter open · Ctrl+C cancel",
        style=questionary.Style([
            ("qmark", "fg:ansicyan bold"), ("pointer", "fg:ansicyan bold"),
            ("highlighted", "fg:ansicyan bold"), ("answer", "fg:ansicyan"),
            ("instruction", "fg:ansibrightblack"), ("text", "fg:ansibrightblack"),
        ]),
        color_depth=ColorDepth.DEPTH_1_BIT if console.no_color or console.color_system is None else None,
    ).ask(kbi_msg="Cancelled.")


def make_console():
    return Console(highlight=False)
