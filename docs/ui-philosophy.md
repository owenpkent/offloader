# Interface philosophy

The desktop app is an instrument on a cart, not a web page. A DIT glances at
it between other tasks, often in a dim room, often while a card is already
copying. Every layout decision is tested against the rules below, in order.

## 1. The whole job fits on one screen

At 1280 × 800 nothing the operator needs sits under a scrollbar. If a control
does not fit, it does not belong on the main screen. The only things allowed
to scroll are lists whose length we do not control: the drive list and the
queue table.

The job panel is deliberately *not* wrapped in a scroll area. If a change
makes it overflow, the overflow clips and a screenshot shows it. That is the
point: a layout that silently scrolls has hidden a design problem, a layout
that clips has reported one.

## 2. Three decisions, then go

Source, destinations, Start. Everything else has a sensible default and is
remembered between runs in `settings.json`. The operator never configures
anything before the first offload, and never configures the same thing twice.

There are no presets. A preset is a name for "the settings I used last time",
and the app already remembers those.

## 3. Disclosure is a different surface, not a longer page

Secondary controls open somewhere that does not push the primary controls
around. The Advanced section is a compact two-column grid that fits beneath
the Start row. The full queue table expands in place of the queue strip.

Only one secondary surface is open at a time: opening Advanced collapses the
queue table and expanding the queue table closes Advanced. Both are one click
to reopen, and the screen can never overflow because of them. A reader who
wonders why the other one closed should find this paragraph.

## 4. State is visible without reading

LEDs, meters, the header readout, and the colour of the status word carry the
state. The header lamp breathes while a job runs. A capacity bar turns amber at
80 % and red at 95 %. A failed job is red before the word "failed" is read. The
eye should get the answer before the brain does.

The queue strip shows the running job's name, stage, meter and throughput at
all times, so the queue table is for managing jobs, not for finding out what
is happening.

## 5. Density over whitespace

Fixed heights, two-column forms, 28-pixel controls in the Advanced grid,
uppercase micro-labels instead of headings. An audio plugin fits forty
controls into a window this size; the app asks for a dozen.

## 6. Nothing destructive in one click, nothing to confirm when nothing is at risk

Cancel asks nothing: a cancelled offload deletes its partial file and the
source is untouched. Quitting with a job running asks. A destination inside
the source is refused rather than confirmed. A card that was already offloaded
warns once, naming the earlier job.

## Visual language

Borrowed from audio-plugin design, because that is the other place where
people stare at a dense dark panel for hours:

- Near-black ground (`#0b0d11`), graphite rails (`#12151b`) with a one-pixel
  lighter top edge so they read as raised.
- One accent, cyan (`#3fd8ff`), used only for things that move data: the Start
  button, the running meter, a card badge, an armed source. Green, amber and
  red are reserved for verdicts.
- Uppercase letter-spaced labels for sections, monospace readouts for paths,
  sizes and percentages, proportional type for everything a human wrote.
- Segmented meters with a soft glow rather than flat progress bars.

The tokens live in `src/offloader/gui/theme.py`. New widgets use those roles
(`section`, `readout`, `badge`, ghost buttons, rail cards) rather than inline
stylesheets, so a palette change is one file.

## How to check a change

Render both states offscreen and look at them:

```sh
QT_QPA_PLATFORM=offscreen python -c "..."   # grab() the window at 1280x800
```

A change that needs a taller window, a scrollbar, or a smaller font to fit has
failed rule 1 and should be redesigned, not accommodated.
