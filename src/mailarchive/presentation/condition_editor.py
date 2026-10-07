"""Edit complete rule predicates without projecting away saved conditions."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from tkinter import ttk

from mailarchive.domain.configuration import Condition, MailField, MatchMode
from mailarchive.presentation.scrollable_frame import ScrollableFrame
from mailarchive.presentation.ui_text import FIELD_LABELS, OPERATOR_LABELS, _label_for


class ConditionRow(ttk.LabelFrame):
    def __init__(
        self, parent: tk.Misc, condition: Condition, remove: Callable[[ConditionRow], None]
    ) -> None:
        super().__init__(parent, padding=8)
        self.field_var = tk.StringVar(self, _label_for(FIELD_LABELS, condition.field))
        self.operator_var = tk.StringVar(self, _label_for(OPERATOR_LABELS, condition.operator))
        self.value_var = tk.StringVar(self, condition.value)
        self.columnconfigure(1, weight=1)
        self.remove_button = ttk.Button(self, text="Remove", command=lambda: remove(self))
        self.remove_button.grid(row=0, column=1, sticky="e", pady=(0, 4))
        ttk.Label(self, text="When").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
        self.field_box = ttk.Combobox(
            self, textvariable=self.field_var, values=list(FIELD_LABELS), state="readonly", width=1
        )
        self.field_box.grid(row=1, column=1, sticky="ew", pady=3)
        self.field_box.bind("<<ComboboxSelected>>", self._update_fields)
        ttk.Label(self, text="Comparison").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        self.operator_box = ttk.Combobox(
            self,
            textvariable=self.operator_var,
            values=list(OPERATOR_LABELS),
            state="readonly",
            width=1,
        )
        self.operator_box.grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(self, text="Value").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        self.value_entry = ttk.Entry(self, textvariable=self.value_var, width=1)
        self.value_entry.grid(row=3, column=1, sticky="ew", pady=3)
        self.hint = ttk.Label(self)
        self.hint.grid(row=4, column=1, sticky="ew", pady=(0, 3))
        self.hint.configure(width=1)
        self.hint.bind(
            "<Configure>", lambda event: self.hint.configure(wraplength=max(1, event.width))
        )
        self._update_fields()

    def _update_fields(self, _event=None) -> None:
        field = FIELD_LABELS[self.field_var.get()]
        self.operator_box.configure(
            state="disabled" if field in {MailField.ALL, MailField.HAS_ATTACHMENT} else "readonly"
        )
        self.value_entry.configure(state="disabled" if field == MailField.ALL else "normal")
        self.hint.configure(
            text="This condition matches every email."
            if field == MailField.ALL
            else 'Enter "Yes" or "No".'
            if field == MailField.HAS_ATTACHMENT
            else "Matching is case-insensitive."
        )

    def condition(self) -> Condition:
        return Condition(
            FIELD_LABELS[self.field_var.get()],
            OPERATOR_LABELS[self.operator_var.get()],
            self.value_var.get(),
        )


class ConditionsEditor(ttk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        conditions: Sequence[Condition],
        match_mode: MatchMode,
        changed: Callable[[], None],
    ) -> None:
        super().__init__(parent)
        self.changed = changed
        self.rows: list[ConditionRow] = []
        self.mode_var = tk.StringVar(self, match_mode.value)
        self.columnconfigure(0, weight=1)
        options = ttk.Frame(self)
        options.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(options, text="Match conditions:").pack(side="left", padx=(0, 8))
        for text, mode in (("All (AND)", MatchMode.ALL), ("Any (OR)", MatchMode.ANY)):
            ttk.Radiobutton(options, text=text, variable=self.mode_var, value=mode.value).pack(
                side="left", padx=(0, 8)
            )
        self.scroll = ScrollableFrame(self)
        self.scroll.grid(row=1, column=0, sticky="ew")
        self.scroll.content.columnconfigure(0, weight=1)
        self.empty_hint = ttk.Label(
            self.scroll.content, text="No conditions: this rule matches all mail."
        )
        self.add_button = ttk.Button(self, text="Add condition", command=self.add_condition)
        self.add_button.grid(row=2, column=0, sticky="w", pady=(6, 0))
        for condition in conditions:
            self.rows.append(ConditionRow(self.scroll.content, condition, self.remove_condition))
        self._layout()

    def _layout(self) -> None:
        self.empty_hint.grid_remove()
        for index, row in enumerate(self.rows):
            row.configure(text=f"Condition {index + 1}")
            row.grid(row=index, column=0, sticky="ew", pady=(0, 6))
        if not self.rows:
            self.empty_hint.grid(row=0, column=0, sticky="w", pady=6)
        self.scroll.bind_widgets()
        self.update_idletasks()
        visible = self.rows[:2] or [self.empty_hint]
        self.scroll.canvas.configure(height=sum(row.winfo_reqheight() + 6 for row in visible))
        self.changed()

    def add_condition(self) -> None:
        row = ConditionRow(
            self.scroll.content, Condition(field=MailField.SUBJECT), self.remove_condition
        )
        self.rows.append(row)
        self._layout()
        row.field_box.focus_set()

    def remove_condition(self, row: ConditionRow) -> None:
        index = self.rows.index(row)
        self.rows.remove(row)
        row.destroy()
        self._layout()
        if self.rows:
            self.rows[min(index, len(self.rows) - 1)].field_box.focus_set()
        else:
            self.add_button.focus_set()

    def matching(self) -> tuple[tuple[Condition, ...], MatchMode]:
        return tuple(row.condition() for row in self.rows), MatchMode(self.mode_var.get())
