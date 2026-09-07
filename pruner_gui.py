#!/usr/bin/env python3
"""Visual front end for the GraphWalker 150% model pruner.

The GUI imports the pruning functions directly from ``prune_graphwalker_model``.

"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from prune_graphwalker_model import (
    PruningError,
    extract_features_from_requirements,
    iter_models_and_elements,
    prune_model,
)


APP_TITLE = "GraphWalker Pruner"
JSON_FILE_TYPES = [("JSON files", "*.json"), ("All files", "*.*")]


def load_json_object(path: Path) -> dict[str, Any]:
    """Read a UTF-8 JSON file and require an object at the root."""

    with path.open("r", encoding="utf-8-sig") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object at its root.")
    return value


def discover_features(model_data: dict[str, Any]) -> list[str]:
    """Return every FEATURE marker found in models, vertices, and edges."""

    features: set[str] = set()
    for _model, _location, element in iter_models_and_elements(model_data):
        features.update(
            extract_features_from_requirements(element.get("requirements", []))
        )
    return sorted(features)


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """Write JSON through a temporary file so a failed write cannot corrupt output."""

    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}_",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as file:
            json.dump(value, file, indent=2, ensure_ascii=False)
            file.write("\n")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


class PrunerApplication:
    """Tkinter application for selecting files, features, and running pruning."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1040x620")
        self.root.minsize(860, 520)

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.status_text = tk.StringVar(value="Select a 150% GraphWalker model to begin.")
        self.feature_summary = tk.StringVar(value="Selected features: none")

        self.model_data: dict[str, Any] | None = None
        self.feature_variables: dict[str, tk.BooleanVar] = {}
        self.last_output_path: Path | None = None
        self.is_running = False
        self.worker_queue: queue.Queue[tuple[Any, ...]] = queue.Queue()

        self._configure_style()
        self._build_interface()
        self._bind_shortcuts()

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        available = style.theme_names()
        for preferred in ("vista", "xpnative", "clam"):
            if preferred in available:
                try:
                    style.theme_use(preferred)
                    break
                except tk.TclError:
                    continue
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel", font=("Segoe UI", 10))
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Run.TButton", font=("Segoe UI", 10, "bold"), padding=(18, 8))

    def _build_interface(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        header = ttk.Frame(main)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Create a product-specific 100% model from a variability-aware GraphWalker model.",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        files = ttk.LabelFrame(main, text="Files", style="Section.TLabelframe", padding=12)
        files.grid(row=1, column=0, sticky="ew")
        files.columnconfigure(1, weight=1)
        self._file_row(files, 0, "Input 150% model:", self.input_path, self.browse_input)
        self._file_row(files, 1, "Output 100% model:", self.output_path, self.browse_output)

        features = ttk.LabelFrame(main, text="Product features", style="Section.TLabelframe", padding=8)
        features.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        features.rowconfigure(1, weight=1)
        features.columnconfigure(0, weight=1)

        feature_toolbar = ttk.Frame(features)
        feature_toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        feature_toolbar.columnconfigure(0, weight=1)
        ttk.Label(feature_toolbar, textvariable=self.feature_summary).grid(row=0, column=0, sticky="w")
        ttk.Button(feature_toolbar, text="Select all", command=self.select_all_features).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(feature_toolbar, text="Clear", command=self.clear_features).grid(row=0, column=2, padx=(6, 0))

        feature_container = ttk.Frame(features)
        feature_container.grid(row=1, column=0, sticky="nsew")
        feature_container.rowconfigure(0, weight=1)
        feature_container.columnconfigure(0, weight=1)

        self.feature_canvas = tk.Canvas(
            feature_container,
            highlightthickness=1,
            highlightbackground="#c9c9c9",
            background="white",
        )
        self.feature_canvas.grid(row=0, column=0, sticky="nsew")
        feature_scrollbar = ttk.Scrollbar(
            feature_container,
            orient="vertical",
            command=self.feature_canvas.yview,
        )
        feature_scrollbar.grid(row=0, column=1, sticky="ns")
        self.feature_canvas.configure(yscrollcommand=feature_scrollbar.set)

        self.feature_inner = ttk.Frame(self.feature_canvas, padding=8)
        self.feature_window = self.feature_canvas.create_window(
            (0, 0), window=self.feature_inner, anchor="nw"
        )
        self.feature_inner.bind("<Configure>", self._on_feature_inner_configure)
        self.feature_canvas.bind("<Configure>", self._on_feature_canvas_configure)
        self.feature_canvas.bind("<Enter>", self._bind_mousewheel)
        self.feature_canvas.bind("<Leave>", self._unbind_mousewheel)
        self._show_feature_placeholder("Choose an input model to discover its FEATURE annotations.")

        controls = ttk.Frame(main)
        controls.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        controls.columnconfigure(1, weight=1)
        self.open_folder_button = ttk.Button(
            controls,
            text="Open output folder",
            command=self.open_output_folder,
            state="disabled",
        )
        self.open_folder_button.grid(row=0, column=0, sticky="w")
        self.run_button = ttk.Button(
            controls,
            text="Run pruning",
            command=self.start_pruning,
            style="Run.TButton",
        )
        self.run_button.grid(row=0, column=2, sticky="e")

        status_bar = ttk.Separator(main, orient="horizontal")
        status_bar.grid(row=4, column=0, sticky="ew", pady=(10, 5))
        ttk.Label(main, textvariable=self.status_text, anchor="w").grid(row=5, column=0, sticky="ew")

    def _file_row(
        self,
        parent: ttk.LabelFrame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: Any,
    ) -> None:
        ttk.Label(parent, text=label, width=23).grid(row=row, column=0, sticky="w", pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky="ew", padx=(0, 8), pady=4)
        ttk.Button(parent, text="Browse...", command=command, width=12).grid(row=row, column=2, pady=4)

    def _bind_shortcuts(self) -> None:
        self.root.bind_all("<Control-o>", lambda _event: self.browse_input())
        self.root.bind_all("<Control-Shift-S>", lambda _event: self.browse_output())
        self.root.bind_all("<F5>", lambda _event: self.start_pruning())

    def _on_feature_inner_configure(self, _event: tk.Event[Any]) -> None:
        self.feature_canvas.configure(scrollregion=self.feature_canvas.bbox("all"))

    def _on_feature_canvas_configure(self, event: tk.Event[Any]) -> None:
        self.feature_canvas.itemconfigure(self.feature_window, width=event.width)

    def _bind_mousewheel(self, _event: tk.Event[Any]) -> None:
        self.feature_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event: tk.Event[Any]) -> None:
        self.feature_canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event[Any]) -> None:
        self.feature_canvas.yview_scroll(int(-event.delta / 120), "units")

    def browse_input(self) -> None:
        initial = self._initial_directory(self.input_path.get())
        selected = filedialog.askopenfilename(
            title="Select GraphWalker input model",
            initialdir=initial,
            filetypes=JSON_FILE_TYPES,
        )
        if not selected:
            return
        self.input_path.set(selected)
        input_file = Path(selected)
        if not self.output_path.get().strip():
            self.output_path.set(str(input_file.with_name(f"{input_file.stem}_pruned.json")))
        self.load_model_and_features()

    def browse_output(self) -> None:
        initial = self._initial_directory(self.output_path.get() or self.input_path.get())
        initial_name = Path(self.output_path.get()).name if self.output_path.get().strip() else "pruned_model.json"
        selected = filedialog.asksaveasfilename(
            title="Choose output GraphWalker model",
            initialdir=initial,
            initialfile=initial_name,
            defaultextension=".json",
            filetypes=JSON_FILE_TYPES,
        )
        if selected:
            self.output_path.set(selected)

    @staticmethod
    def _initial_directory(value: str) -> str:
        if not value:
            return str(Path.cwd())
        path = Path(value).expanduser()
        return str(path if path.is_dir() else path.parent)

    def load_model_and_features(self) -> None:
        path = Path(self.input_path.get().strip()).expanduser()
        try:
            model_data = load_json_object(path)
            if not isinstance(model_data.get("models"), list):
                raise ValueError("The input JSON does not contain a GraphWalker 'models' list.")
            features = discover_features(model_data)
            self.model_data = model_data
            selected_existing = {
                name for name, variable in self.feature_variables.items() if variable.get()
            }
            self._populate_features(features, selected_existing)
            self.status_text.set(f"Loaded {path.name}; select the features for the product model.")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            self.model_data = None
            self._show_feature_placeholder("The input model could not be loaded.")
            self._show_error("Input model error", error)

    def _populate_features(self, features: list[str], selected: set[str]) -> None:
        for child in self.feature_inner.winfo_children():
            child.destroy()
        self.feature_variables.clear()

        if not features:
            self._show_feature_placeholder("No FEATURE annotations were found in this model.")
            self._update_feature_summary()
            return

        columns = 3 if len(features) >= 6 else 2 if len(features) >= 3 else 1
        for column in range(columns):
            self.feature_inner.columnconfigure(column, weight=1, uniform="features")

        for index, feature in enumerate(features):
            variable = tk.BooleanVar(value=feature in selected)
            variable.trace_add("write", lambda *_args: self._update_feature_summary())
            self.feature_variables[feature] = variable
            row, column = divmod(index, columns)
            ttk.Checkbutton(
                self.feature_inner,
                text=feature,
                variable=variable,
            ).grid(row=row, column=column, sticky="w", padx=(4, 20), pady=4)

        self._update_feature_summary()

    def _show_feature_placeholder(self, text: str) -> None:
        for child in self.feature_inner.winfo_children():
            child.destroy()
        ttk.Label(self.feature_inner, text=text, foreground="#666666").grid(
            row=0, column=0, sticky="nw", padx=4, pady=4
        )

    def _selected_features(self) -> set[str]:
        return {feature for feature, variable in self.feature_variables.items() if variable.get()}

    def _update_feature_summary(self) -> None:
        selected = sorted(self._selected_features())
        total = len(self.feature_variables)
        if selected:
            self.feature_summary.set(
                f"Selected features ({len(selected)}/{total}): {', '.join(selected)}"
            )
        else:
            self.feature_summary.set(f"Selected features (0/{total}): none")

    def select_all_features(self) -> None:
        for variable in self.feature_variables.values():
            variable.set(True)

    def clear_features(self) -> None:
        for variable in self.feature_variables.values():
            variable.set(False)

    def start_pruning(self) -> None:
        if self.is_running:
            return
        try:
            input_path, output_path, selected_features, effective_config = self._validate_run_inputs()
        except ValueError as error:
            messagebox.showerror("Cannot run pruning", str(error), parent=self.root)
            return

        self.is_running = True
        self.run_button.configure(state="disabled")
        self.open_folder_button.configure(state="disabled")
        self.status_text.set("Pruning in progress...")
        worker = threading.Thread(
            target=self._run_pruning_worker,
            args=(
                input_path,
                output_path,
                selected_features,
                effective_config,
            ),
            daemon=True,
        )
        worker.start()
        self.root.after(100, self._poll_worker_queue)

    def _validate_run_inputs(
        self,
    ) -> tuple[Path, Path, set[str], dict[str, Any]]:
        input_text = self.input_path.get().strip()
        output_text = self.output_path.get().strip()
        if not input_text:
            raise ValueError("Select an input GraphWalker JSON model.")
        if not output_text:
            raise ValueError("Choose an output JSON model path.")

        input_path = Path(input_text).expanduser()
        output_path = Path(output_text).expanduser()
        if not input_path.is_file():
            raise ValueError(f"The input model does not exist:\n{input_path}")
        if input_path.resolve() == output_path.resolve():
            raise ValueError("The output model must not overwrite the 150% input model.")
        if output_path.suffix.lower() != ".json":
            raise ValueError("The output model must use the .json extension.")

        selected_features = self._selected_features()
        if self.feature_variables and not selected_features:
            raise ValueError("Select at least one product feature before running the pruner.")

        config = {"strict": True}
        return input_path, output_path, selected_features, config

    def _run_pruning_worker(
        self,
        input_path: Path,
        output_path: Path,
        selected_features: set[str],
        effective_config: dict[str, Any],
    ) -> None:
        try:
            model_data = load_json_object(input_path)
            pruned_model = prune_model(
                model_data,
                selected_features,
                effective_config,
            )

            write_json_atomic(output_path, pruned_model)
            self.worker_queue.put(("success", output_path))
        except (OSError, json.JSONDecodeError, PruningError, ValueError) as error:
            self.worker_queue.put(("error", str(error), traceback.format_exc()))
        except Exception as error:  # Prevent a background-thread failure from vanishing.
            self.worker_queue.put(("error", str(error), traceback.format_exc()))

    def _poll_worker_queue(self) -> None:
        try:
            message = self.worker_queue.get_nowait()
        except queue.Empty:
            if self.is_running:
                self.root.after(100, self._poll_worker_queue)
            return

        if message[0] == "success":
            _, output_path = message
            self._pruning_succeeded(output_path)
        else:
            _, error_message, details = message
            self._pruning_failed(RuntimeError(error_message), details)

    def _pruning_succeeded(
        self,
        output_path: Path,
    ) -> None:
        self._finish_run()
        self.last_output_path = output_path
        self.open_folder_button.configure(state="normal")

        self.status_text.set(f"Completed. Output written to {output_path.name}.")
        messagebox.showinfo(
            "Pruning completed",
            f"The product-specific model was created successfully.\n\n{output_path}",
            parent=self.root,
        )

    def _pruning_failed(self, error: Exception, _details: str) -> None:
        self._finish_run()
        self.status_text.set(f"Pruning failed: {error}")
        messagebox.showerror("Pruning failed", str(error), parent=self.root)

    def _finish_run(self) -> None:
        self.is_running = False
        self.run_button.configure(state="normal")

    def _show_error(self, title: str, error: Exception) -> None:
        self.status_text.set(str(error))
        messagebox.showerror(title, str(error), parent=self.root)

    def open_output_folder(self) -> None:
        path = self.last_output_path or (
            Path(self.output_path.get().strip()).expanduser()
            if self.output_path.get().strip()
            else None
        )
        if path is None:
            return
        folder = path.parent
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except OSError as error:
            self._show_error("Could not open folder", error)

def main() -> None:
    root = tk.Tk()
    PrunerApplication(root)
    root.mainloop()


if __name__ == "__main__":
    main()
