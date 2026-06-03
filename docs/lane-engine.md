# Lane Engine Notes

The graph pane lays out commits from newest to oldest using a topo-ordered commit list.

For each commit row, the server tracks active lanes, removes the current commit from the lane stack, inserts parent commits, and emits only row-local line segments:

- incoming segment from a child to the current dot
- outgoing segments from the dot to each parent
- pass-through segments for other active branch lanes

That keeps the browser renderer simple and makes the line math testable without a browser.

The visual renderer intentionally keeps labels sparse. Branch heads are always labeled, ordinary commits use short subjects, and full metadata lives on hover.
