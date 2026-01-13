import { useState } from "react"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Button } from "@/components/ui/button"
import { BookOpen, Key, Bot, ChevronRight, Sparkles, ArrowRight, ToggleRight, Mail, MessageSquare } from "lucide-react"
import { cn } from "@/lib/utils"

interface GettingStartedModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
}

type ArticleId = "gmail-quickstart" | "build-agent" | "share-credentials" | "conversation-vs-building"

interface Article {
  id: ArticleId
  title: string
  icon: React.ReactNode
  content: (onNavigate: (id: ArticleId) => void) => React.ReactNode
}

const articles: Article[] = [
  {
    id: "gmail-quickstart",
    title: "Gmail Agent Quick Start",
    icon: <Mail className="h-4 w-4" />,
    content: (onNavigate) => (
      <div className="space-y-5">
        <p className="text-sm text-muted-foreground leading-relaxed">
          Set up your first Gmail parsing agent in 3 simple steps. Click any step to learn more.
        </p>

        {/* Flow Steps */}
        <div className="flex flex-col gap-3">
          {/* Step 1 */}
          <button
            onClick={() => onNavigate("share-credentials")}
            className="group relative p-4 rounded-xl border-2 border-amber-200 dark:border-amber-800 bg-gradient-to-br from-amber-50 to-orange-50 dark:from-amber-950/40 dark:to-orange-950/40 hover:border-amber-400 dark:hover:border-amber-600 transition-all text-left"
          >
            <div className="flex items-start gap-4">
              <div className="flex-shrink-0 w-10 h-10 rounded-full bg-amber-500 text-white flex items-center justify-center font-bold text-lg shadow-md">
                1
              </div>
              <div className="flex-1 min-w-0">
                <h4 className="font-semibold text-amber-700 dark:text-amber-300 flex items-center gap-2">
                  Prepare Your Credentials
                  <ArrowRight className="h-4 w-4 opacity-0 group-hover:opacity-100 transition-opacity" />
                </h4>
                <p className="text-sm text-amber-600/80 dark:text-amber-400/80 mt-1">
                  Go to <strong>Sidebar → Credentials</strong> and add Gmail OAuth (ReadOnly) to authorize email access.
                </p>
              </div>
              <Key className="h-5 w-5 text-amber-500 dark:text-amber-400 flex-shrink-0" />
            </div>
          </button>

          {/* Connector */}
          <div className="flex justify-center">
            <div className="w-0.5 h-4 bg-gradient-to-b from-amber-300 to-violet-300 dark:from-amber-600 dark:to-violet-600" />
          </div>

          {/* Step 2 */}
          <button
            onClick={() => onNavigate("build-agent")}
            className="group relative p-4 rounded-xl border-2 border-violet-200 dark:border-violet-800 bg-gradient-to-br from-violet-50 to-purple-50 dark:from-violet-950/40 dark:to-purple-950/40 hover:border-violet-400 dark:hover:border-violet-600 transition-all text-left"
          >
            <div className="flex items-start gap-4">
              <div className="flex-shrink-0 w-10 h-10 rounded-full bg-violet-500 text-white flex items-center justify-center font-bold text-lg shadow-md">
                2
              </div>
              <div className="flex-1 min-w-0">
                <h4 className="font-semibold text-violet-700 dark:text-violet-300 flex items-center gap-2">
                  Create Agent & Share Credentials
                  <ArrowRight className="h-4 w-4 opacity-0 group-hover:opacity-100 transition-opacity" />
                </h4>
                <p className="text-sm text-violet-600/80 dark:text-violet-400/80 mt-1">
                  Click <strong>+ New Agent</strong>, describe your goal, and select credentials during agent creation.
                </p>
              </div>
              <Bot className="h-5 w-5 text-violet-500 dark:text-violet-400 flex-shrink-0" />
            </div>
          </button>

          {/* Connector */}
          <div className="flex justify-center">
            <div className="w-0.5 h-4 bg-gradient-to-b from-violet-300 to-green-300 dark:from-violet-600 dark:to-green-600" />
          </div>

          {/* Step 3 */}
          <button
            onClick={() => onNavigate("conversation-vs-building")}
            className="group relative p-4 rounded-xl border-2 border-green-200 dark:border-green-800 bg-gradient-to-br from-green-50 to-emerald-50 dark:from-green-950/40 dark:to-emerald-950/40 hover:border-green-400 dark:hover:border-green-600 transition-all text-left"
          >
            <div className="flex items-start gap-4">
              <div className="flex-shrink-0 w-10 h-10 rounded-full bg-green-500 text-white flex items-center justify-center font-bold text-lg shadow-md">
                3
              </div>
              <div className="flex-1 min-w-0">
                <h4 className="font-semibold text-green-700 dark:text-green-300 flex items-center gap-2">
                  Start Conversation
                  <ArrowRight className="h-4 w-4 opacity-0 group-hover:opacity-100 transition-opacity" />
                </h4>
                <p className="text-sm text-green-600/80 dark:text-green-400/80 mt-1">
                  Chat in <strong>Building mode</strong> to create scripts, then switch to <strong>Conversation mode</strong> for daily use.
                </p>
              </div>
              <MessageSquare className="h-5 w-5 text-green-500 dark:text-green-400 flex-shrink-0" />
            </div>
          </button>
        </div>
      </div>
    ),
  },
  {
    id: "build-agent",
    title: "How to Build An Agent",
    icon: <Bot className="h-4 w-4" />,
    content: (onNavigate) => (
      <div className="space-y-4">
        <div>
          <h3 className="font-medium text-base mb-2">Writing Your First Build Prompt</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            To create an agent, select <strong>+ New Agent</strong> and describe what you want it to do.
            Be specific about the goal and expected output. The system will create scripts and workflows
            that run whenever you start a conversation.
          </p>
        </div>

        <div className="bg-muted/50 rounded-lg p-4 border">
          <p className="text-xs font-medium text-muted-foreground mb-2">Example Build Prompt:</p>
          <p className="text-sm italic">
            "Create an agent that checks my Gmail inbox for unread emails and provides a brief
            summary of each message, including sender, subject, and a one-line preview."
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">How It Works</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            The building agent will analyze your request, create Python scripts for each task
            (like fetching emails, parsing content), and set up an entry point prompt. Once built,
            you can run your agent in <strong>Conversation mode</strong> with a simple trigger like
            "Check my unread emails."
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">Tips for Better Agents</h3>
          <ul className="text-sm text-muted-foreground space-y-1.5 list-disc list-inside">
            <li>Focus on one clear goal per agent</li>
            <li>Mention specific services or APIs you want to use</li>
            <li>Describe the output format you prefer (summary, list, report)</li>
          </ul>
        </div>

        {/* Accented credentials callout */}
        <button
          onClick={() => onNavigate("share-credentials")}
          className="w-full mt-2 p-3 rounded-lg border-2 border-amber-200 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 hover:bg-amber-100 dark:hover:bg-amber-900/40 transition-colors text-left group"
        >
          <div className="flex items-center gap-2">
            <Key className="h-4 w-4 text-amber-600 dark:text-amber-400" />
            <span className="text-sm font-medium text-amber-700 dark:text-amber-300">
              Need access to external services?
            </span>
            <ArrowRight className="h-4 w-4 text-amber-600 dark:text-amber-400 ml-auto opacity-0 group-hover:opacity-100 transition-opacity" />
          </div>
          <p className="text-xs text-amber-600/80 dark:text-amber-400/80 mt-1 ml-6">
            Set up credentials first so your agent can connect to Gmail, calendars, and other services.
          </p>
        </button>
      </div>
    ),
  },
  {
    id: "share-credentials",
    title: "How to Share Credentials",
    icon: <Key className="h-4 w-4" />,
    content: () => (
      <div className="space-y-4">
        <div>
          <h3 className="font-medium text-base mb-2">Why Credentials Matter</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            Agents need access to external services to perform useful tasks. For example, an email
            summary agent needs Gmail access, while a calendar agent needs Google Calendar permissions.
            Credentials are stored securely and only accessed by your agent's scripts.
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">Setting Up Gmail Access</h3>
          <p className="text-sm text-muted-foreground leading-relaxed mb-2">
            To let your agent read emails, create a credential with <strong>Gmail Readonly OAuth</strong> type.
            This grants read-only access to your inbox - the agent cannot send or delete emails.
          </p>
          <ol className="text-sm text-muted-foreground space-y-1.5 list-decimal list-inside">
            <li>In the sidebar menu, select <strong>Credentials</strong></li>
            <li>Click <strong>Add Credential</strong></li>
            <li>Select <strong>Gmail Readonly OAuth</strong> as the type</li>
            <li>Authorize with your Google account</li>
            <li>Share credentials with your agent during the first message or later in the agent configuration</li>
          </ol>
        </div>

        <div className="bg-muted/50 rounded-lg p-4 border">
          <p className="text-xs font-medium text-muted-foreground mb-2">Once Linked:</p>
          <p className="text-sm">
            When you build an agent that requires Gmail, it will automatically detect the available
            credential and use it. You'll be able to run prompts like "Summarize my unread emails"
            without any additional setup.
          </p>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">Other Credential Types</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            You can add various credential types including API keys, OAuth tokens for different services,
            and custom configurations. Check the Credentials page for all available options.
          </p>
        </div>
      </div>
    ),
  },
  {
    id: "conversation-vs-building",
    title: "Conversation vs Building",
    icon: <ToggleRight className="h-4 w-4" />,
    content: () => (
      <div className="space-y-4">
        <div>
          <h3 className="font-medium text-base mb-2">Two Modes, One Agent</h3>
          <p className="text-sm text-muted-foreground leading-relaxed">
            Every agent supports two modes that you can toggle using the switch on the dashboard.
            Each mode is optimized for different tasks and uses different AI models behind the scenes.
          </p>
        </div>

        <div className="grid gap-4">
          {/* Building Mode */}
          <div className="p-4 rounded-lg border-2 border-orange-200 dark:border-orange-800 bg-orange-50 dark:bg-orange-950/30">
            <div className="flex items-center gap-2 mb-2">
              <div className="h-6 w-11 rounded-full bg-orange-400 relative">
                <div className="absolute right-0.5 top-0.5 h-5 w-5 rounded-full bg-white" />
              </div>
              <h4 className="font-medium text-orange-700 dark:text-orange-300">Building Mode</h4>
            </div>
            <p className="text-sm text-orange-600/80 dark:text-orange-400/80">
              Use this mode to <strong>set up your agent</strong>. The AI will create Python scripts,
              configure integrations, and define workflows. It uses a more powerful model optimized
              for code generation and complex reasoning.
            </p>
            <p className="text-xs text-orange-600/60 dark:text-orange-400/60 mt-2">
              Best for: Initial setup, adding new capabilities, debugging, maintenance
            </p>
          </div>

          {/* Conversation Mode */}
          <div className="p-4 rounded-lg border-2 border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-950/30">
            <div className="flex items-center gap-2 mb-2">
              <div className="h-6 w-11 rounded-full bg-gray-300 dark:bg-gray-600 relative">
                <div className="absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white" />
              </div>
              <h4 className="font-medium text-green-700 dark:text-green-300">Conversation Mode</h4>
            </div>
            <p className="text-sm text-green-600/80 dark:text-green-400/80">
              Use this mode for <strong>daily operations</strong>. The agent executes pre-built scripts
              and workflows, then presents results in natural language. It uses a faster, more
              cost-effective model.
            </p>
            <p className="text-xs text-green-600/60 dark:text-green-400/60 mt-2">
              Best for: Running tasks, getting reports, quick queries
            </p>
          </div>
        </div>

        <div>
          <h3 className="font-medium text-base mb-2">Typical Workflow</h3>
          <ol className="text-sm text-muted-foreground space-y-1.5 list-decimal list-inside">
            <li>Start in <strong>Building mode</strong> to describe what you want</li>
            <li>The agent creates scripts and sets up an entry point</li>
            <li>Switch to <strong>Conversation mode</strong> for daily use</li>
            <li>Return to Building mode anytime to add features or fix issues</li>
          </ol>
        </div>
      </div>
    ),
  },
]

export function GettingStartedModal({ open, onOpenChange }: GettingStartedModalProps) {
  const [selectedArticle, setSelectedArticle] = useState<ArticleId>("gmail-quickstart")

  const currentArticle = articles.find((a) => a.id === selectedArticle) || articles[0]

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[800px] max-h-[80vh] p-0 overflow-hidden">
        <div className="flex h-full max-h-[80vh]">
          {/* Left Sidebar - Table of Contents */}
          <div className="w-56 shrink-0 border-r bg-muted/30 p-4 flex flex-col">
            <DialogHeader className="pb-4">
              <DialogTitle className="flex items-center gap-2 text-base">
                <BookOpen className="h-5 w-5 text-violet-500" />
                Getting Started
              </DialogTitle>
            </DialogHeader>

            <nav className="space-y-1 flex-1">
              {articles.map((article) => (
                <button
                  key={article.id}
                  onClick={() => setSelectedArticle(article.id)}
                  className={cn(
                    "w-full flex items-center gap-2 px-3 py-2 text-sm rounded-md transition-colors text-left",
                    selectedArticle === article.id
                      ? "bg-violet-100 dark:bg-violet-900/30 text-violet-700 dark:text-violet-300 font-medium"
                      : "text-muted-foreground hover:bg-muted hover:text-foreground"
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
                Start Building
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
