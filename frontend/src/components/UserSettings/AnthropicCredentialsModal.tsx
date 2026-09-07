import {
  BookOpen,
  ChevronRight,
  Clock,
  ExternalLink,
  Key,
  Shield,
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

type ArticleId = "setup-via-api" | "setup-via-oauth"

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
            . They provide programmatic access to Claude models and are ideal
            for production applications, automation, and CI/CD pipelines.
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">How to Get an API Key</h3>
          <ol className="text-sm text-muted-foreground space-y-2 list-decimal list-inside">
            <li>
              Visit{" "}
              <a
                href="https://console.anthropic.com"
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
    id: "setup-via-oauth",
    title: "Setup via Claude Code OAuth",
    icon: <Shield className="h-5 w-5" />,
    content: () => (
      <div className="space-y-4">
        <div>
          <h3 className="font-medium text-base mb-2">What are OAuth Tokens?</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            Claude Code OAuth tokens are specialized authentication tokens that
            start with{" "}
            <code className="px-1.5 py-0.5 rounded bg-muted font-mono text-xs">
              sk-ant-oat
            </code>
            . These tokens are designed for Claude Code CLI integration and
            provide seamless authentication for development workflows.
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">
            How to Get an OAuth Token
          </h3>
          <p className="text-sm text-muted-foreground leading-relaxed mb-3">
            OAuth tokens are generated through the Claude Code CLI on your local
            machine. Follow these steps to obtain your token:
          </p>
          <ol className="text-sm text-muted-foreground space-y-2 list-decimal list-inside">
            <li>
              <a
                href="https://claude.com/product/claude-code"
                target="_blank"
                rel="noopener noreferrer"
                className="text-violet-600 dark:text-violet-400 hover:underline inline-flex items-center gap-1"
              >
                Install Claude Code CLI
                <ExternalLink className="h-3 w-3" />
              </a>{" "}
              on your local machine
            </li>
            <li>
              Run the setup command:{" "}
              <code className="px-1.5 py-0.5 rounded bg-muted font-mono text-xs">
                claude setup-token
              </code>
            </li>
            <li>
              Authorize with your Anthropic subscription account in the browser
            </li>
            <li>
              <strong>Copy the token from the console immediately</strong> -
              once you close the window, you won't be able to see the token
              again
            </li>
            <li>
              Paste the token into this credential form to save it securely
            </li>
          </ol>
        </div>

        <div className="bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800 rounded-lg p-3">
          <div className="flex items-start gap-2">
            <Clock className="h-4 w-4 text-amber-600 dark:text-amber-400 mt-0.5 flex-shrink-0" />
            <div>
              <p className="text-xs font-medium text-amber-700 dark:text-amber-300 mb-1">
                Token Expiration
              </p>
              <p className="text-xs text-amber-600/80 dark:text-amber-400/80">
                OAuth tokens typically expire after <strong>1 year</strong>. Set
                an expiry notification date when adding the credential to
                receive a reminder before it expires.
              </p>
            </div>
          </div>
        </div>

        <div className="bg-violet-50 dark:bg-violet-950/30 border-2 border-violet-200 dark:border-violet-800 rounded-lg p-4">
          <div className="flex items-start gap-2">
            <Shield className="h-4 w-4 text-violet-600 dark:text-violet-400 mt-0.5 flex-shrink-0" />
            <div>
              <p className="text-xs font-medium text-violet-700 dark:text-violet-300 mb-1">
                Token Security
              </p>
              <p className="text-xs text-violet-600/80 dark:text-violet-400/80">
                OAuth tokens are scoped credentials that provide secure access
                to Claude services. Like API keys, they should be kept
                confidential and never shared publicly.
              </p>
            </div>
          </div>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">
            Adding Your OAuth Token
          </h3>
          <ol className="text-sm text-muted-foreground space-y-1.5 list-decimal list-inside">
            <li>
              In the credential dialog, select <strong>Anthropic</strong> as the
              type
            </li>
            <li>
              Paste your OAuth token starting with{" "}
              <code className="px-1 py-0.5 rounded bg-muted font-mono text-xs">
                sk-ant-oat
              </code>
            </li>
            <li>Give it a descriptive name (e.g., "Claude Code OAuth")</li>
            <li>Save the credential</li>
          </ol>
        </div>

        <div className="bg-muted/50 rounded-lg p-4 border">
          <p className="text-xs font-medium text-muted-foreground mb-2">
            Example OAuth Token Format:
          </p>
          <div className="flex items-center gap-2">
            {/* A format example, not a value: the two ghost "copy" buttons that
                used to sit here copied the placeholder itself and did nothing
                (an empty onClick), which is worse than no control at all. */}
            <code className="text-xs font-mono bg-background px-2 py-1 rounded border flex-1">
              sk-ant-oat01-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
            </code>
          </div>
        </div>

        <div className="bg-blue-50 dark:bg-blue-950/30 border border-blue-200 dark:border-blue-800 rounded-lg p-3">
          <p className="text-xs text-blue-600 dark:text-blue-400">
            <strong>Note:</strong> The system automatically detects the
            credential type based on the prefix (
            <code className="px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 font-mono">
              sk-ant-api
            </code>{" "}
            vs{" "}
            <code className="px-1 py-0.5 rounded bg-blue-100 dark:bg-blue-900 font-mono">
              sk-ant-oat
            </code>
            ) and configures the appropriate environment variables.
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
