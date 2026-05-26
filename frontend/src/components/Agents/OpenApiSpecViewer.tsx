import { ChevronDown, ChevronRight, Server } from "lucide-react"
import { useMemo, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"

import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"

/**
 * A lightweight, read-only OpenAPI (3.x) viewer built from the app's own
 * primitives — no external docs library. It renders a harvested ``agent_api``
 * spec object (fetched authenticated elsewhere) as comfy, themed docs:
 * grouped operations, method badges, parameter tables, request/response
 * schemas with ``$ref`` resolution. Intentionally minimal — no playground,
 * no branding, no client export.
 */

// OpenAPI documents are dynamically shaped; we lean on `any` deliberately.
type AnySchema = Record<string, any>

interface OpenApiSpec {
  openapi?: string
  info?: { title?: string; version?: string; description?: string }
  servers?: Array<{ url?: string; description?: string }>
  paths?: Record<string, Record<string, any>>
  components?: { schemas?: Record<string, AnySchema> }
  $defs?: Record<string, AnySchema>
}

const HTTP_METHODS = [
  "get",
  "post",
  "put",
  "patch",
  "delete",
  "head",
  "options",
] as const

const METHOD_STYLES: Record<string, string> = {
  get: "bg-emerald-500",
  post: "bg-sky-500",
  put: "bg-amber-500",
  patch: "bg-violet-500",
  delete: "bg-rose-500",
  head: "bg-gray-400",
  options: "bg-gray-400",
}

interface Operation {
  key: string
  path: string
  method: string
  op: AnySchema
  pathParams: AnySchema[]
  tag: string
}

// ----------------------------------------------------------------------------
// $ref resolution
// ----------------------------------------------------------------------------

/** Resolves a local ``$ref`` (``#/components/schemas/X`` or ``#/$defs/X``). */
function resolveRef(spec: OpenApiSpec, ref: string): AnySchema | null {
  if (!ref.startsWith("#/")) return null
  const parts = ref.slice(2).split("/")
  let node: any = spec
  for (const part of parts) {
    if (node == null) return null
    node =
      node[decodeURIComponent(part.replace(/~1/g, "/").replace(/~0/g, "~"))]
  }
  return node ?? null
}

/** Last segment of a ``$ref`` — used as a human-friendly type name. */
function refName(ref: string): string {
  const parts = ref.split("/")
  return parts[parts.length - 1] || "object"
}

/** Follows a single ``$ref`` one level (keeps the ref name for labelling). */
function deref(
  spec: OpenApiSpec,
  schema: AnySchema | undefined,
): { schema: AnySchema; name?: string } {
  if (!schema) return { schema: {} }
  if (typeof schema.$ref === "string") {
    const resolved = resolveRef(spec, schema.$ref)
    return { schema: resolved ?? {}, name: refName(schema.$ref) }
  }
  return { schema }
}

// ----------------------------------------------------------------------------
// type labelling
// ----------------------------------------------------------------------------

/** A short, human-readable type label for a schema (resolves one ref level). */
function typeLabel(spec: OpenApiSpec, schema: AnySchema | undefined): string {
  if (!schema) return "any"
  if (typeof schema.$ref === "string") return refName(schema.$ref)

  // Combinators
  for (const key of ["anyOf", "oneOf"] as const) {
    if (Array.isArray(schema[key])) {
      const labels = schema[key]
        .filter((s: AnySchema) => s?.type !== "null")
        .map((s: AnySchema) => typeLabel(spec, s))
      const nullable = schema[key].some((s: AnySchema) => s?.type === "null")
      const joined = Array.from(new Set(labels)).join(" | ") || "any"
      return nullable ? `${joined} | null` : joined
    }
  }
  if (Array.isArray(schema.allOf)) {
    return schema.allOf.map((s: AnySchema) => typeLabel(spec, s)).join(" & ")
  }

  const t = Array.isArray(schema.type)
    ? schema.type.filter((x: string) => x !== "null").join(" | ")
    : schema.type

  if (t === "array") {
    const items = schema.items
    return `${typeLabel(spec, items)}[]`
  }
  if (schema.enum) return t || "enum"
  if (schema.format) return `${t || "string"}<${schema.format}>`
  return t || "object"
}

// ----------------------------------------------------------------------------
// constraint badges
// ----------------------------------------------------------------------------

function constraintChips(schema: AnySchema): string[] {
  const chips: string[] = []
  const add = (label: string, v: unknown) => {
    if (v !== undefined && v !== null) chips.push(`${label}: ${v}`)
  }
  add("default", schema.default)
  add("min", schema.minimum ?? schema.exclusiveMinimum)
  add("max", schema.maximum ?? schema.exclusiveMaximum)
  add("minLen", schema.minLength)
  add("maxLen", schema.maxLength)
  add("minItems", schema.minItems)
  add("maxItems", schema.maxItems)
  if (schema.pattern) chips.push(`pattern: ${schema.pattern}`)
  if (schema.format && !chips.length) chips.push(schema.format)
  return chips
}

// ----------------------------------------------------------------------------
// schema tree
// ----------------------------------------------------------------------------

function Markdown({
  content,
  className,
}: {
  content: string
  className?: string
}) {
  return (
    <div
      className={cn(
        "prose prose-sm dark:prose-invert max-w-none prose-p:my-1 prose-pre:my-1",
        className,
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  )
}

function EnumChips({ values }: { values: any[] }) {
  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {values.map((v) => (
        <code
          key={String(v)}
          className="rounded bg-muted px-1.5 py-0.5 text-[11px] font-mono"
        >
          {JSON.stringify(v)}
        </code>
      ))}
    </div>
  )
}

/** Recursively renders the properties of an (object/array) schema. */
function SchemaView({
  spec,
  schema,
  depth = 0,
  seen = new Set<string>(),
}: {
  spec: OpenApiSpec
  schema: AnySchema | undefined
  depth?: number
  seen?: Set<string>
}) {
  if (!schema) return null
  if (depth > 8) {
    return <p className="text-xs text-muted-foreground italic">…</p>
  }

  // Resolve a top-level ref, guarding against cycles.
  if (typeof schema.$ref === "string") {
    const name = refName(schema.$ref)
    if (seen.has(name)) {
      return (
        <p className="text-xs text-muted-foreground italic">
          (recursive {name})
        </p>
      )
    }
    const resolved = resolveRef(spec, schema.$ref)
    return (
      <SchemaView
        spec={spec}
        schema={resolved ?? {}}
        depth={depth}
        seen={new Set(seen).add(name)}
      />
    )
  }

  // allOf — merge object members for a flat property view.
  if (Array.isArray(schema.allOf)) {
    return (
      <div className="space-y-2">
        {schema.allOf.map((sub: AnySchema, i: number) => (
          <SchemaView
            key={i}
            spec={spec}
            schema={sub}
            depth={depth}
            seen={seen}
          />
        ))}
      </div>
    )
  }

  // anyOf / oneOf — list the variants.
  for (const key of ["anyOf", "oneOf"] as const) {
    const variants: AnySchema[] | undefined = schema[key]
    if (Array.isArray(variants)) {
      const real = variants.filter((s) => s?.type !== "null")
      if (real.length === 1) {
        return (
          <SchemaView spec={spec} schema={real[0]} depth={depth} seen={seen} />
        )
      }
      return (
        <div className="space-y-2">
          <p className="text-xs text-muted-foreground">
            {key === "oneOf" ? "One of:" : "Any of:"}
          </p>
          {real.map((sub, i) => (
            <div key={i} className="rounded border border-dashed pl-3 py-1">
              <SchemaView spec={spec} schema={sub} depth={depth} seen={seen} />
            </div>
          ))}
        </div>
      )
    }
  }

  const type = Array.isArray(schema.type) ? schema.type[0] : schema.type

  // Array: describe the item schema.
  if (type === "array" || schema.items) {
    return (
      <div className="space-y-1">
        <p className="text-xs text-muted-foreground">
          Array of{" "}
          <span className="font-mono">{typeLabel(spec, schema.items)}</span>
        </p>
        <div className="border-l pl-3">
          <SchemaView
            spec={spec}
            schema={schema.items}
            depth={depth + 1}
            seen={seen}
          />
        </div>
      </div>
    )
  }

  // Object: list properties.
  const props: Record<string, AnySchema> | undefined = schema.properties
  if (props && Object.keys(props).length > 0) {
    const required: string[] = Array.isArray(schema.required)
      ? schema.required
      : []
    return (
      <div className="divide-y divide-border/60">
        {Object.entries(props).map(([name, propSchema]) => (
          <PropertyRow
            key={name}
            spec={spec}
            name={name}
            schema={propSchema}
            required={required.includes(name)}
            depth={depth}
            seen={seen}
          />
        ))}
      </div>
    )
  }

  // Leaf / primitive with no properties.
  const chips = constraintChips(schema)
  const hasExtras = schema.enum || chips.length || schema.description
  if (!hasExtras) {
    return (
      <p className="text-xs text-muted-foreground">
        <span className="font-mono">{typeLabel(spec, schema)}</span>
      </p>
    )
  }
  return (
    <div className="text-xs text-muted-foreground space-y-1">
      <span className="font-mono">{typeLabel(spec, schema)}</span>
      {schema.description && (
        <Markdown
          content={schema.description}
          className="text-muted-foreground"
        />
      )}
      {chips.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {chips.map((c) => (
            <span key={c} className="rounded bg-muted px-1.5 py-0.5 font-mono">
              {c}
            </span>
          ))}
        </div>
      )}
      {schema.enum && <EnumChips values={schema.enum} />}
    </div>
  )
}

/** One property line in an object schema, expandable when it nests. */
function PropertyRow({
  spec,
  name,
  schema,
  required,
  depth,
  seen,
}: {
  spec: OpenApiSpec
  name: string
  schema: AnySchema
  required: boolean
  depth: number
  seen: Set<string>
}) {
  const { schema: resolved } = deref(spec, schema)
  const type = Array.isArray(resolved.type) ? resolved.type[0] : resolved.type
  const isObject =
    (resolved.properties && Object.keys(resolved.properties).length > 0) ||
    Array.isArray(resolved.allOf) ||
    Array.isArray(resolved.anyOf) ||
    Array.isArray(resolved.oneOf)
  const isArrayOfComplex =
    (type === "array" || resolved.items) &&
    (() => {
      const { schema: item } = deref(spec, resolved.items)
      return !!(item.properties || item.allOf || item.anyOf || item.oneOf)
    })()
  const expandable = (isObject || isArrayOfComplex) && depth < 8
  const [open, setOpen] = useState(false)

  const chips = constraintChips(resolved)

  return (
    <div className="py-2">
      <div className="flex items-start gap-2">
        {expandable ? (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="mt-0.5 text-muted-foreground hover:text-foreground"
            aria-label={open ? "Collapse" : "Expand"}
          >
            {open ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
        ) : (
          <span className="w-3.5 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <code className="text-xs font-semibold">{name}</code>
            <span className="font-mono text-[11px] text-muted-foreground">
              {typeLabel(spec, schema)}
            </span>
            {required && (
              <span className="text-[10px] font-medium uppercase tracking-wide text-rose-500">
                required
              </span>
            )}
            {chips.map((c) => (
              <span
                key={c}
                className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground"
              >
                {c}
              </span>
            ))}
          </div>
          {resolved.description && (
            <Markdown
              content={resolved.description}
              className="mt-0.5 text-xs text-muted-foreground"
            />
          )}
          {resolved.enum && <EnumChips values={resolved.enum} />}
        </div>
      </div>
      {expandable && open && (
        <div className="ml-5 mt-2 border-l pl-3">
          <SchemaView
            spec={spec}
            schema={schema}
            depth={depth + 1}
            seen={seen}
          />
        </div>
      )}
    </div>
  )
}

// ----------------------------------------------------------------------------
// parameters
// ----------------------------------------------------------------------------

function ParameterSection({
  spec,
  params,
}: {
  spec: OpenApiSpec
  params: AnySchema[]
}) {
  if (!params.length) return null
  const groups = ["path", "query", "header", "cookie"]
  const byLocation = groups
    .map((loc) => ({ loc, items: params.filter((p) => p.in === loc) }))
    .filter((g) => g.items.length > 0)

  return (
    <div className="space-y-3">
      {byLocation.map(({ loc, items }) => (
        <div key={loc}>
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {loc} parameters
          </p>
          <div className="divide-y divide-border/60 rounded-md border">
            {items.map((p) => {
              const { schema: pSchema } = deref(spec, p.schema)
              const chips = constraintChips(pSchema)
              return (
                <div key={`${loc}:${p.name}`} className="px-3 py-2">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <code className="text-xs font-semibold">{p.name}</code>
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {typeLabel(spec, p.schema)}
                    </span>
                    {p.required && (
                      <span className="text-[10px] font-medium uppercase tracking-wide text-rose-500">
                        required
                      </span>
                    )}
                    {chips.map((c) => (
                      <span
                        key={c}
                        className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground"
                      >
                        {c}
                      </span>
                    ))}
                  </div>
                  {p.description && (
                    <Markdown
                      content={p.description}
                      className="mt-0.5 text-xs text-muted-foreground"
                    />
                  )}
                  {pSchema.enum && <EnumChips values={pSchema.enum} />}
                </div>
              )
            })}
          </div>
        </div>
      ))}
    </div>
  )
}

// ----------------------------------------------------------------------------
// request body & responses
// ----------------------------------------------------------------------------

function BodySchema({
  spec,
  content,
}: {
  spec: OpenApiSpec
  content: Record<string, AnySchema> | undefined
}) {
  if (!content) return null
  const mediaTypes = Object.keys(content)
  if (!mediaTypes.length) return null
  // Prefer JSON if present.
  const mt = mediaTypes.find((m) => m.includes("json")) ?? mediaTypes[0]
  const schema = content[mt]?.schema
  return (
    <div className="space-y-1">
      <p className="font-mono text-[11px] text-muted-foreground">{mt}</p>
      {schema ? (
        <SchemaView spec={spec} schema={schema} />
      ) : (
        <p className="text-xs text-muted-foreground italic">No schema</p>
      )}
    </div>
  )
}

function statusColor(code: string): string {
  if (code.startsWith("2"))
    return "border-emerald-500/40 text-emerald-600 dark:text-emerald-400"
  if (code.startsWith("3"))
    return "border-sky-500/40 text-sky-600 dark:text-sky-400"
  if (code.startsWith("4"))
    return "border-amber-500/40 text-amber-600 dark:text-amber-400"
  if (code.startsWith("5"))
    return "border-rose-500/40 text-rose-600 dark:text-rose-400"
  return "border-border text-muted-foreground"
}

function ResponsesSection({
  spec,
  responses,
}: {
  spec: OpenApiSpec
  responses: Record<string, AnySchema> | undefined
}) {
  if (!responses || !Object.keys(responses).length) return null
  return (
    <div className="space-y-2">
      {Object.entries(responses).map(([code, resp]) => (
        <div key={code} className="rounded-md border">
          <div className="flex items-center gap-2 border-b px-3 py-1.5">
            <span
              className={cn(
                "rounded border px-1.5 py-0.5 text-xs font-mono font-semibold",
                statusColor(code),
              )}
            >
              {code}
            </span>
            {resp?.description && (
              <span className="text-xs text-muted-foreground">
                {resp.description}
              </span>
            )}
          </div>
          {resp?.content && (
            <div className="px-3 py-2">
              <BodySchema spec={spec} content={resp.content} />
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ----------------------------------------------------------------------------
// operation row
// ----------------------------------------------------------------------------

function Section({
  title,
  children,
}: {
  title: string
  children: React.ReactNode
}) {
  return (
    <div className="space-y-1.5">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      {children}
    </div>
  )
}

function OperationRow({
  spec,
  operation,
  open,
  onToggle,
}: {
  spec: OpenApiSpec
  operation: Operation
  open: boolean
  onToggle: () => void
}) {
  const { op, method, path } = operation
  // Merge path-level + operation-level parameters, resolving any parameter
  // ``$ref`` to its object (which still carries ``in`` / ``name``).
  const params: AnySchema[] = [
    ...operation.pathParams,
    ...(Array.isArray(op.parameters) ? op.parameters : []),
  ].map((p) =>
    typeof p.$ref === "string" ? (resolveRef(spec, p.$ref) ?? p) : p,
  )

  return (
    <div className="overflow-hidden rounded-lg border">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-muted/50"
      >
        <span
          className={cn(
            "shrink-0 rounded px-2 py-0.5 text-[11px] font-bold uppercase text-white",
            METHOD_STYLES[method] ?? "bg-gray-400",
          )}
        >
          {method}
        </span>
        <code className="text-sm font-medium">{path}</code>
        {op.summary && (
          <span className="ml-1 truncate text-xs text-muted-foreground">
            {op.summary}
          </span>
        )}
        {op.deprecated && (
          <Badge variant="outline" className="ml-auto text-[10px]">
            deprecated
          </Badge>
        )}
        {open ? (
          <ChevronDown
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground",
              !op.deprecated && "ml-auto",
            )}
          />
        ) : (
          <ChevronRight
            className={cn(
              "h-4 w-4 shrink-0 text-muted-foreground",
              !op.deprecated && "ml-auto",
            )}
          />
        )}
      </button>
      {open && (
        <div className="space-y-4 border-t bg-muted/20 px-4 py-3">
          {op.description && <Markdown content={op.description} />}
          {params.length > 0 && (
            <Section title="Parameters">
              <ParameterSection spec={spec} params={params} />
            </Section>
          )}
          {op.requestBody && (
            <Section title="Request body">
              <BodySchema spec={spec} content={op.requestBody.content} />
            </Section>
          )}
          <Section title="Responses">
            <ResponsesSection spec={spec} responses={op.responses} />
          </Section>
        </div>
      )}
    </div>
  )
}

// ----------------------------------------------------------------------------
// main viewer
// ----------------------------------------------------------------------------

export function OpenApiSpecViewer({
  spec: rawSpec,
}: {
  spec: Record<string, unknown>
}) {
  // The spec is fetched as an opaque JSON object; treat it as an OpenAPI doc.
  const spec = rawSpec as OpenApiSpec
  const operations = useMemo<Operation[]>(() => {
    const out: Operation[] = []
    const paths = spec.paths ?? {}
    for (const [path, pathItem] of Object.entries(paths)) {
      const pathParams: AnySchema[] = Array.isArray(pathItem.parameters)
        ? pathItem.parameters
        : []
      for (const method of HTTP_METHODS) {
        const op = pathItem[method]
        if (!op || typeof op !== "object") continue
        out.push({
          key: `${method}:${path}`,
          path,
          method,
          op,
          pathParams,
          tag:
            Array.isArray(op.tags) && op.tags.length ? op.tags[0] : "Endpoints",
        })
      }
    }
    return out
  }, [spec])

  const groups = useMemo(() => {
    const map = new Map<string, Operation[]>()
    for (const o of operations) {
      const list = map.get(o.tag) ?? []
      list.push(o)
      map.set(o.tag, list)
    }
    return Array.from(map.entries())
  }, [operations])

  const [openKeys, setOpenKeys] = useState<Record<string, boolean>>({})
  const allOpen =
    operations.length > 0 && operations.every((o) => openKeys[o.key])

  const toggleAll = () => {
    if (allOpen) {
      setOpenKeys({})
      return
    }
    const next: Record<string, boolean> = {}
    for (const o of operations) next[o.key] = true
    setOpenKeys(next)
  }

  const info = spec.info ?? {}

  return (
    <div className="mx-auto max-w-4xl px-4 py-6 sm:px-6">
      {/* Header */}
      <header className="mb-6 space-y-3 border-b pb-5">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold">
            {info.title || "Agent API"}
          </h1>
          {info.version && (
            <Badge variant="secondary" className="font-mono">
              v{info.version}
            </Badge>
          )}
          {spec.openapi && (
            <span className="text-xs text-muted-foreground">
              OpenAPI {spec.openapi}
            </span>
          )}
        </div>
        {info.description && <Markdown content={info.description} />}
        {Array.isArray(spec.servers) && spec.servers.length > 0 && (
          <div className="space-y-1">
            {spec.servers.map((s) => (
              <div
                key={s.url}
                className="flex items-center gap-2 text-xs text-muted-foreground"
              >
                <Server className="h-3.5 w-3.5" />
                <code>{s.url}</code>
                {s.description && <span>— {s.description}</span>}
              </div>
            ))}
          </div>
        )}
      </header>

      {operations.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          This API exposes no endpoints yet.
        </p>
      ) : (
        <>
          <div className="mb-3 flex items-center justify-between">
            <p className="text-xs text-muted-foreground">
              {operations.length} endpoint{operations.length === 1 ? "" : "s"}
            </p>
            <button
              type="button"
              onClick={toggleAll}
              className="text-xs font-medium text-muted-foreground hover:text-foreground"
            >
              {allOpen ? "Collapse all" : "Expand all"}
            </button>
          </div>
          <div className="space-y-6">
            {groups.map(([tag, ops]) => (
              <section key={tag} className="space-y-2">
                {groups.length > 1 && (
                  <h2 className="text-sm font-semibold text-muted-foreground">
                    {tag}
                  </h2>
                )}
                <div className="space-y-2">
                  {ops.map((operation) => (
                    <OperationRow
                      key={operation.key}
                      spec={spec}
                      operation={operation}
                      open={!!openKeys[operation.key]}
                      onToggle={() =>
                        setOpenKeys((prev) => ({
                          ...prev,
                          [operation.key]: !prev[operation.key],
                        }))
                      }
                    />
                  ))}
                </div>
              </section>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
