from datetime import datetime

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


class Dashboard:
    def __init__(self):
        self.stats = {
            "status": "Starting...",
            "vacancies_total": 0,
            "vacancies_pending": 0,
            "news_total": 0,
            "news_unread": 0,
            "spool_pending": 0,
            "spool_sent": 0,
            "spool_failed": 0,
            "trigger_job": "unknown",
            "dispatcher_job": "unknown",
            "gateway_status": "unknown",
            "outbox_total": 0,
            "last_outbox": "None",
            "last_action": "None",
            "last_error": "None",
            "last_update": "Never",
        }

    def update_stats(self, **kwargs):
        self.stats.update(kwargs)
        self.stats["last_update"] = datetime.now().strftime("%H:%M:%S")

    def _build_core_table(self) -> Table:
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right", style="green")
        table.add_row("Total Vacancies", str(self.stats["vacancies_total"]))
        table.add_row("Pending Vacancies", str(self.stats["vacancies_pending"]), style="bold yellow")
        table.add_row("Total News", str(self.stats["news_total"]))
        table.add_row("Unread News", str(self.stats["news_unread"]), style="bold blue")
        table.add_row("Last Action", self.stats["last_action"], style="italic white")
        return table

    def _build_runtime_table(self) -> Table:
        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Runtime", style="cyan")
        table.add_column("Value", justify="right", style="green")
        table.add_row("Spool Pending", str(self.stats["spool_pending"]), style="bold yellow")
        table.add_row("Spool Sent", str(self.stats["spool_sent"]))
        table.add_row("Spool Failed", str(self.stats["spool_failed"]), style="bold red")
        table.add_row("Trigger Job", self.stats["trigger_job"])
        table.add_row("Dispatcher Job", self.stats["dispatcher_job"])
        table.add_row("Gateway", self.stats["gateway_status"])
        return table

    def generate_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )
        layout["main"].split_row(Layout(name="left"), Layout(name="right"))

        status_color = "green" if "Online" in self.stats["status"] else "yellow"
        header_content = Panel(
            Text(f"🚀 parserUserBot | Status: {self.stats['status']}", justify="center", style=f"bold {status_color}"),
            style="blue",
        )
        layout["header"].update(header_content)

        layout["left"].update(Panel(self._build_core_table(), title="📊 Telegram / DB"))
        layout["right"].update(Panel(self._build_runtime_table(), title="⚙️ Runtime / Callback"))

        footer_content = Panel(
            Text(
                f"Last updated: {self.stats['last_update']} | Last error: {self.stats['last_error']} | Press Ctrl+C to stop",
                justify="center",
                style="dim",
            ),
            style="white",
        )
        layout["footer"].update(footer_content)
        return layout
