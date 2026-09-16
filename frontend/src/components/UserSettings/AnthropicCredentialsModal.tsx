import {
  AlertTriangle,
  BookOpen,
  ChevronRight,
  ExternalLink,
  Key,
  Sparkles,
} from "lucide-react"
import { useState } from "react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"

interface AnthropicCredentialsModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

type ArticleId = "setup-via-api" | "oauth-tokens"

interface Article {
  id: ArticleId
  title: string
  icon: React.ReactNode
  content: (onNavigate: (id: ArticleId) => void) => React.ReactNode
}

const articles: Article[] = [
  {
    id: "setup-via-api",
    title: "Setup via API Keys",
    icon: <Key className="h-5 w-5" />,
    content: () => (
      <div className="space-y-4">
        <div>
          <h3 className="font-medium text-base mb-2">What are API Keys?</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            Anthropic API keys are traditional authentication tokens that start
            with{" "}
            <code className="px-1.5 py-0.5 rounded bg-muted font-mono text-xs">
              sk-ant-api
            </code>
            . They provide programmatic access to Claude models and are the
            recommended way to run agents in the cloud: unlike Claude Code OAuth
            tokens, they are not signed out unexpectedly.
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">How to Get an API Key</h3>
          <ol className="text-sm text-muted-foreground space-y-2 list-decimal list-inside">
            <li>
              Visit{" "}
              <a
                href="https://console.anthropic.com/settings/keys"
                target="_blank"
                rel="noopener noreferrer"
                className="text-violet-600 dark:text-violet-400 hover:underline inline-flex items-center gap-1"
              >
                console.anthropic.com
                <ExternalLink className="h-3 w-3" />
              </a>
            </li>
            <li>Sign in or create an Anthropic account</li>
            <li>
              Navigate to <strong>API Keys</strong> section
            </li>
            <li>
              Click <strong>Create Key</strong>
            </li>
            <li>
              Copy your new API key (it starts with{" "}
              <code className="px-1 py-0.5 rounded bg-muted font-mono text-xs">
                sk-ant-api
              </code>
              )
            </li>
          </ol>
        </div>

        <div className="bg-amber-50 dark:bg-amber-950/30 border-2 border-amber-200 dark:border-amber-800 rounded-lg p-4">
          <div className="flex items-start gap-2">
            <Key className="h-4 w-4 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
            <div>
              <p className="text-xs font-medium text-amber-700 dark:text-amber-300 mb-1">
                Important: Store Your Key Securely
              </p>
              <p className="text-xs text-amber-600/80 dark:text-amber-400/80">
                API keys grant full access to your Anthropic account. Never
                commit them to version control or share them publicly. Store
                them in this credential manager for secure access by your
                agents.
              </p>
            </div>
          </div>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">Adding Your API Key</h3>
          <ol className="text-sm text-muted-foreground space-y-1.5 list-decimal list-inside">
            <li>
              In the credential dialog, select <strong>Anthropic</strong> as the
              type
            </li>
            <li>
              Paste your API key starting with{" "}
              <code className="px-1 py-0.5 rounded bg-muted font-mono text-xs">
                sk-ant-api
              </code>
            </li>
            <li>Give it a descriptive name (e.g., "Production Claude Key")</li>
            <li>Save the credential</li>
          </ol>
        </div>

        <div className="bg-muted/50 rounded-lg p-4 border">
          <p className="text-xs font-medium text-muted-foreground mb-2">
            Example API Key Format:
          </p>
          <div className="flex items-center gap-2">
            {/* A format example, not a value: the two ghost "copy" buttons that
                used to sit here copied the placeholder itself and did nothing
                (an empty onClick), which is worse than no control at all. */}
            <code className="text-xs font-mono bg-background px-2 py-1 rounded border flex-1">
              sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
            </code>
          </div>
        </div>
      </div>
    ),
  },
  {
    id: "oauth-tokens",
    title: "Claude Code OAuth Tokens",
    icon: <AlertTriangle className="h-5 w-5" />,
    content: (onNavigate) => (
      <div className="space-y-4">
        <div className="bg-amber-50 dark:bg-amber-950/30 border-2 border-amber-200 dark:border-amber-800 rounded-lg p-4">
          <div className="flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
            <div>
              <p className="text-xs font-medium text-amber-700 dark:text-amber-300 mb-1">
                Not recommended for cloud agents
              </p>
              <p className="text-xs text-amber-600/80 dark:text-amber-400/80">
                Tokens created with{" "}
                <code className="px-1 py-0.5 rounded bg-amber-100 dark:bg-amber-900 font-mono">
                  claude setup-token
                </code>{" "}
                are tied to a Claude subscription login and can be signed out
                unexpectedly. When that happens, every agent using the
                credential stops working until you create a new token.
              </p>
            </div>
          </div>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">What to do instead</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            Create an Anthropic API key (it starts with{" "}
            <code className="px-1.5 py-0.5 rounded bg-muted font-mono text-xs">
              sk-ant-api
            </code>
            ) and use that for your agents. See{" "}
            <button
              type="button"
              onClick={() => onNavigate("setup-via-api")}
              className="text-violet-600 dark:text-violet-400 hover:underline"
            >
              Setup via API Keys
            </button>
            .
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">
            Already using an OAuth token?
          </h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            Credentials starting with{" "}
            <code className="px-1.5 py-0.5 rounded bg-muted font-mono text-xs">
              sk-ant-oat
            </code>{" "}
            still work. To switch, edit the credential and paste an API key in
            its place. Your agents keep the same credential, so you don&apos;t
            need to change anything else.
          </p>
        </div>
      </div>
    ),
  },
]

export function AnthropicCredentialsModal({
  open,
  onOpenChange,
}: AnthropicCredentialsModalProps) {
  const [selectedArticle, setSelectedArticle] =
    useState<ArticleId>("setup-via-api")

  const currentArticle =
    articles.find((a) => a.id === selectedArticle) || articles[0]

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[800px] max-h-[80vh] p-0 overflow-hidden">
        <div className="flex h-full max-h-[80vh]">
          {/* Left Sidebar - Table of Contents */}
          <div className="w-64 shrink-0 border-r bg-muted/30 p-4 flex flex-col">
            <DialogHeader className="pb-4">
              <DialogTitle className="flex items-center gap-2 text-base">
                <BookOpen className="h-5 w-5 text-violet-500" />
                Anthropic Setup Guide
              </DialogTitle>
            </DialogHeader>

            <nav className="space-y-1 flex-1">
              {articles.map((article) => (
                <button
                  type="button"
                  key={article.id}
                  onClick={() => setSelectedArticle(article.id)}
                  className={cn(
                    "w-full flex items-center gap-2 px-3 py-2 text-sm rounded-md transition-colors text-left",
                    selectedArticle === article.id
                      ? "bg-violet-100 dark:bg-violet-900/30 text-violet-700 dark:text-violet-300 font-medium"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground",
                  )}
                >
                  {article.icon}
                  <span className="flex-1">{article.title}</span>
                  {selectedArticle === article.id && (
                    <ChevronRight className="h-4 w-4" />
                  )}
                </button>
              ))}
            </nav>

            <div className="pt-4 border-t mt-4">
              <Button
                onClick={() => onOpenChange(false)}
                className="w-full bg-gradient-to-r from-violet-600 to-purple-600 hover:from-violet-700 hover:to-purple-700"
              >
                <Sparkles className="h-4 w-4 mr-2" />
                Got It
              </Button>
            </div>
          </div>

          {/* Right Content Area */}
          <div className="flex-1 p-6 overflow-y-auto">
            <div className="flex items-center gap-2 mb-4">
              {currentArticle.icon}
              <h2 className="text-lg font-semibold">{currentArticle.title}</h2>
            </div>
            {currentArticle.content(setSelectedArticle)}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
