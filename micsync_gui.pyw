#!/usr/bin/env python3
"""
micsync_gui - a window for micsync.py.

Add the recordings of one take (file picker, folder picker, or drag and drop),
choose which mic is the reference and where the results should go, then Sync.

Start it by double-clicking this file, or:   python micsync_gui.pyw

Needs micsync.py in the same folder, plus:   pip install numpy scipy soundfile
Drag and drop also needs (optional):         pip install tkinterdnd2
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace

try:  # optional: without it everything works except drag and drop
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAVE_DND = True
except Exception:
    HAVE_DND = False

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import micsync
    IMPORT_ERROR = None
except Exception as e:  # numpy/scipy/soundfile missing, or micsync.py not next to this file
    micsync, IMPORT_ERROR = None, e

SUFFIX = "synced"


class Cancelled(BaseException):
    """Raised inside the worker to stop a run. Not an Exception, so micsync's
    per-track error handling does not swallow it."""


class App:
    def __init__(self, root):
        self.root = root
        self.files = []          # Paths, in list order
        self.reference = None    # one of self.files
        self.worker = None
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.last_out = None     # folder to open when a run finishes

        root.title("micsync - sync multi-mic recordings")
        root.minsize(720, 560)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=3)
        root.rowconfigure(6, weight=2)
        pad = dict(padx=10, pady=4)

        hint = ("Drag the WAV files (or the folder) of one take here, or use the buttons."
                if HAVE_DND else "Add the WAV files of one take with the buttons below.")
        ttk.Label(root, text=hint + " Double-click a file to make it the reference.").grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 2))

        # ---- file list
        box = ttk.Frame(root)
        box.grid(row=1, column=0, sticky="nsew", **pad)
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(box, columns=("role", "name", "folder"), show="headings", selectmode="extended", height=6)
        for col, text, width, stretch in (("role", "Role", 90, False), ("name", "File", 240, True), ("folder", "Folder", 320, True)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self.use_selected_as_reference())
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        if HAVE_DND:
            for widget in (root, self.tree):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)

        # ---- list buttons
        row = ttk.Frame(root)
        row.grid(row=2, column=0, sticky="ew", **pad)
        self.list_buttons = [
            ttk.Button(row, text="Add files...", command=self.pick_files),
            ttk.Button(row, text="Add folder...", command=self.pick_folder),
            ttk.Button(row, text="Use as reference", command=self.use_selected_as_reference),
            ttk.Button(row, text="Remove", command=self.remove_selected),
            ttk.Button(row, text="Clear", command=self.clear),
        ]
        for b in self.list_buttons:
            b.pack(side="left", padx=(0, 6))

        # ---- output folder
        out = ttk.LabelFrame(root, text="Save synced files")
        out.grid(row=3, column=0, sticky="ew", **pad)
        out.columnconfigure(1, weight=1)
        self.out_mode = tk.StringVar(value="beside")
        self.out_dir = tk.StringVar()
        ttk.Radiobutton(out, text="Next to the original files", variable=self.out_mode, value="beside",
                        command=self.refresh).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(4, 0))
        ttk.Radiobutton(out, text="In this folder:", variable=self.out_mode, value="folder",
                        command=self.refresh).grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self.out_entry = ttk.Entry(out, textvariable=self.out_dir)
        self.out_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.out_browse = ttk.Button(out, text="Browse...", command=self.pick_out_dir)
        self.out_browse.grid(row=1, column=2, padx=8, pady=4)

        # ---- options
        opts = ttk.Frame(root)
        opts.grid(row=4, column=0, sticky="ew", **pad)
        self.follow = tk.BooleanVar(value=True)
        self.flip = tk.BooleanVar(value=True)
        self.opt_boxes = [
            ttk.Checkbutton(opts, text="Follow movement between mics (recommended)", variable=self.follow),
            ttk.Checkbutton(opts, text="Correct inverted polarity", variable=self.flip),
        ]
        for c in self.opt_boxes:
            c.pack(side="left", padx=(0, 16))

        # ---- run
        run = ttk.Frame(root)
        run.grid(row=5, column=0, sticky="ew", **pad)
        run.columnconfigure(2, weight=1)
        self.sync_btn = ttk.Button(run, text="Sync", command=self.start)
        self.sync_btn.grid(row=0, column=0)
        self.cancel_btn = ttk.Button(run, text="Cancel", command=self.request_cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1, padx=6)
        self.bar = ttk.Progressbar(run, maximum=1.0)
        self.bar.grid(row=0, column=2, sticky="ew", padx=6)
        self.state = tk.StringVar(value="Add at least two files.")
        ttk.Label(run, textvariable=self.state, width=34, anchor="w").grid(row=0, column=3, padx=(6, 0))

        # ---- report
        rep = ttk.Frame(root)
        rep.grid(row=6, column=0, sticky="nsew", **pad)
        rep.columnconfigure(0, weight=1)
        rep.rowconfigure(0, weight=1)
        self.log = tk.Text(rep, height=10, wrap="none", state="disabled", font=("Consolas", 9) if os.name == "nt" else "TkFixedFont")
        self.log.grid(row=0, column=0, sticky="nsew")
        ys = ttk.Scrollbar(rep, orient="vertical", command=self.log.yview)
        ys.grid(row=0, column=1, sticky="ns")
        xs = ttk.Scrollbar(rep, orient="horizontal", command=self.log.xview)
        xs.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.open_btn = ttk.Button(root, text="Open output folder", command=self.open_out, state="disabled")
        self.open_btn.grid(row=7, column=0, sticky="e", padx=10, pady=(0, 10))

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh()
        self.root.after(100, self.poll)

    # ------------------------------------------------------------ the file list

    def add_paths(self, paths):
        """Add files and/or folders. A folder adds its WAV files, except ones already ending in -synced."""
        skipped = []
        have = {p.resolve() for p in self.files}
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                found = sorted((q for q in p.iterdir() if q.is_file() and q.suffix.lower() == ".wav"
                                and not q.stem.endswith(f"-{SUFFIX}")), key=micsync.natural_key)
            elif p.is_file() and p.suffix.lower() == ".wav":
                found = [p]
            else:
                found = []
                skipped.append(p.name)
            for q in found:
                if q.resolve() not in have:
                    have.add(q.resolve())
                    self.files.append(q)
        if self.reference is None and self.files:
            self.reference = self.files[0]
        self.refresh()
        if skipped:
            self.state.set(f"Not WAV files, skipped: {len(skipped)}")

    def pick_files(self):
        names = filedialog.askopenfilenames(title="Add recordings", filetypes=[("WAV audio", "*.wav"), ("All files", "*.*")])
        if names:
            self.add_paths(names)

    def pick_folder(self):
        name = filedialog.askdirectory(title="Add every WAV in a folder")
        if name:
            self.add_paths([name])

    def on_drop(self, event):
        self.add_paths(self.root.tk.splitlist(event.data))

    def selected(self):
        return [self.files[int(i)] for i in self.tree.selection()]

    def use_selected_as_reference(self):
        if self.running():
            return
        sel = self.selected()
        if sel:
            self.reference = sel[0]
            self.refresh()

    def remove_selected(self):
        if self.running():
            return
        gone = set(self.selected())
        self.files = [p for p in self.files if p not in gone]
        if self.reference in gone:
            self.reference = self.files[0] if self.files else None
        self.refresh()

    def clear(self):
        self.files, self.reference = [], None
        self.refresh()

    def refresh(self):
        """Redraw the list and enable or disable controls to match the current state."""
        keep = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.files):
            role = "Reference" if p == self.reference else ""
            self.tree.insert("", "end", iid=str(i), values=(role, p.name, str(p.parent)))
        self.tree.selection_set([i for i in keep if self.tree.exists(i)])
        busy = self.running()
        for w in self.list_buttons + self.opt_boxes:
            w.configure(state="disabled" if busy else "normal")
        folder = self.out_mode.get() == "folder" and not busy
        self.out_entry.configure(state="normal" if folder else "disabled")
        self.out_browse.configure(state="normal" if folder else "disabled")
        self.sync_btn.configure(state="normal" if len(self.files) >= 2 and not busy else "disabled")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        if not busy:
            n = len(self.files)
            self.state.set("Add at least two files." if n < 2 else f"Ready: {n - 1} file(s) to sync to the reference.")

    # ------------------------------------------------------------------ output

    def pick_out_dir(self):
        name = filedialog.askdirectory(title="Folder for the synced files")
        if name:
            self.out_dir.set(name)

    def open_out(self):
        if not self.last_out:
            return
        try:
            if os.name == "nt":
                os.startfile(self.last_out)  # noqa: only exists on Windows
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.last_out)])
        except Exception as e:
            messagebox.showerror("micsync", f"Could not open {self.last_out}:\n{e}")

    # --------------------------------------------------------------------- run

    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def start(self):
        if self.running() or len(self.files) < 2:
            return
        out_dir = None
        if self.out_mode.get() == "folder":
            if not self.out_dir.get().strip():
                messagebox.showwarning("micsync", "Choose the folder to save the synced files in, or pick \"Next to the original files\".")
                return
            out_dir = Path(self.out_dir.get().strip())
        reference = self.reference
        tracks = [p for p in self.files if p != reference]
        args = SimpleNamespace(interval=1.0, max_offset=1.0, suffix=SUFFIX,
                               steady=not self.follow.get(), no_flip=not self.flip.get())
        self.last_out = out_dir or reference.parent
        self.cancel.clear()
        self.set_log("")
        self.bar.configure(value=0)
        self.open_btn.configure(state="disabled")
        self.worker = threading.Thread(target=self.work, args=(reference, tracks, args, out_dir), daemon=True)
        self.worker.start()
        self.refresh()
        self.state.set("Starting...")

    def work(self, reference, tracks, args, out_dir):
        """Runs in a background thread; talks to the window only through self.events."""
        def check():
            if self.cancel.is_set():
                raise Cancelled()

        def say(text):
            check()
            self.events.put(("log", text))

        def show(text):
            check()
            if text:
                self.events.put(("state", text.strip()))

        def tick(done):
            check()
            self.events.put(("bar", done))

        try:
            failed = micsync.sync_files(reference, tracks, args, out_dir, say=say, show=show, tick=tick)
            self.events.put(("done", (len(tracks) - failed, failed)))
        except Cancelled:
            self.events.put(("cancelled", None))
        except micsync.SyncError as e:
            self.events.put(("error", str(e)))
        except Exception as e:  # anything unexpected: show it instead of dying silently
            self.events.put(("error", f"{type(e).__name__}: {e}"))

    def request_cancel(self):
        if self.running():
            self.cancel.set()
            self.state.set("Cancelling...")

    def poll(self):
        """Apply everything the worker has reported since the last look."""
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.add_log(value)
                elif kind == "state":
                    self.state.set(value[:1].upper() + value[1:])
                elif kind == "bar":
                    self.bar.configure(value=value)
                else:
                    self.worker.join(timeout=5)
                    self.refresh()
                    if kind == "done":
                        ok, failed = value
                        self.bar.configure(value=1.0)
                        self.state.set(f"Done: {ok} synced" + (f", {failed} skipped (see report)." if failed else "."))
                        self.open_btn.configure(state="normal")
                    elif kind == "cancelled":
                        self.bar.configure(value=0)
                        self.state.set("Cancelled.")
                        self.add_log("\nCancelled. Tracks finished before this point were written.")
                    else:
                        self.bar.configure(value=0)
                        self.state.set("Could not sync.")
                        self.add_log("\n" + value)
                        messagebox.showerror("micsync", value)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def set_log(self, text):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", text)
        self.log.configure(state="disabled")

    def add_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.lstrip("\n") + "\n" if self.log.index("end-1c") == "1.0" else text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_close(self):
        if self.running():
            if not messagebox.askyesno("micsync", "A sync is still running. Stop it and close?"):
                return
            self.cancel.set()
        self.root.destroy()


def selftest():
    """Used by the automated build: open the window briefly and report what works.
    Exit code 0 = fine, 1 = micsync or its libraries failed to load, 2 = no drag and drop."""
    if IMPORT_ERROR is not None:
        return 1
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    app = App(root)
    root.update()
    app.on_close()
    return 0 if HAVE_DND else 2


def main():
    if "--selftest" in sys.argv[1:]:
        return selftest()
    root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    if IMPORT_ERROR is not None:
        root.withdraw()
        messagebox.showerror(
            "micsync",
            f"Could not load micsync.py or one of the libraries it needs:\n\n{IMPORT_ERROR}\n\n"
            "Keep micsync.py in the same folder as this file, and install the libraries with:\n\n"
            "python -m pip install numpy scipy soundfile")
        return 1
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
