# Markdown Rendering

## Purpose

Markdown rendering converts agent text output into formatted HTML within chat messages. It handles inline code, code blocks, lists, tables, links, and other GitHub-flavored markdown features, providing a readable and visually consistent display of agent responses.

## Core Concepts

- **MarkdownRenderer** — Shared component wrapping `react-markdown` with `remark-gfm` plugin and custom code rendering
- **Inline Code** — Single backtick code rendered with subtle background highlight (`bg-slate-900` with theme-adaptive text)
- **Code Blocks** — Triple backtick blocks rendered with dark CLI-like theme (slate-900 background, light text, monospace, horizontal scroll)
- **Prose Classes** — Tailwind Typography (`prose dark:prose-invert`) controls spacing, sizing, and dark mode adaptation

## Where Markdown Renders

| Context | className Configuration |
|---------|------------------------|
| Assistant text events | `prose dark:prose-invert max-w-none prose-p:leading-normal prose-p:my-2 prose-ul:my-2 prose-li:my-0` |
| Thinking blocks | `prose dark:prose-invert max-w-none text-xs` (smaller sizing for all elements) |
| Command responses (`/files`) | Same as assistant text |
| Tool input values (default renderer) | `prose prose-xs dark:prose-invert max-w-none` |

## Business Rules

- GFM (GitHub-flavored markdown) enabled: tables, strikethrough, task lists, autolinks
- Code blocks use `pre` wrapped in `not-prose` div to escape Tailwind Typography sizing
- Language detection via `language-*` CSS class on `code` element — determines block vs inline rendering
- No syntax highlighting library — language class is detected but not used for coloring
- The same MarkdownRenderer component is used across all chat contexts (session page, guest share, webapp widget)

## Integration Points

- **[Chat Windows](chat_windows.md)** — MarkdownRenderer is the primary text display component for agent responses
- **[Tool Rendering](tool_rendering.md)** — Default tool renderer uses MarkdownRenderer for markdown-like tool input values
