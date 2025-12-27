import { useState, useEffect, useRef } from "react"
import { Circle, CheckCircle2, Star } from "lucide-react"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Badge } from "@/components/ui/badge"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"

interface QuestionOption {
  label: string
  description: string
}

interface Question {
  question: string
  header: string
  multiSelect: boolean
  options: QuestionOption[]
}

interface AnswerQuestionsModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  questions: Question[]
  messageId: string
  onSubmit: (answers: string, messageId: string) => void
}

export function AnswerQuestionsModal({
  open,
  onOpenChange,
  questions,
  messageId,
  onSubmit,
}: AnswerQuestionsModalProps) {
  // Track answers: single-select = string, multi-select = string[]
  const [answers, setAnswers] = useState<Record<number, string | string[]>>({})
  // Track custom text inputs
  const [customInputs, setCustomInputs] = useState<Record<number, string>>({})
  // Track which questions have custom input selected
  const [customSelected, setCustomSelected] = useState<Record<number, boolean>>({})
  // Refs for custom input fields
  const customInputRefs = useRef<Record<number, HTMLInputElement | null>>({})
  // Refs for question containers
  const questionRefs = useRef<Record<number, HTMLDivElement | null>>({})

  // Reset state when modal opens/closes
  useEffect(() => {
    if (open) {
      setAnswers({})
      setCustomInputs({})
      setCustomSelected({})
    }
  }, [open])

  // Check if a question is answered
  const isQuestionAnswered = (index: number): boolean => {
    const answer = answers[index]
    if (customSelected[index]) {
      return customInputs[index]?.trim().length > 0
    }
    if (Array.isArray(answer)) {
      return answer.length > 0
    }
    return !!answer
  }

  // Check if all questions are answered
  const allQuestionsAnswered = questions.every((_, index) => isQuestionAnswered(index))

  // Handle single-select answer change
  const handleSingleSelectChange = (questionIndex: number, value: string) => {
    if (value === "__custom__") {
      setCustomSelected({ ...customSelected, [questionIndex]: true })
      setAnswers({ ...answers, [questionIndex]: "" })
      // Focus the input after state update
      setTimeout(() => {
        const inputRef = customInputRefs.current[questionIndex]
        if (inputRef) {
          inputRef.focus()
        }
      }, 0)
    } else {
      setCustomSelected({ ...customSelected, [questionIndex]: false })
      setAnswers({ ...answers, [questionIndex]: value })
    }
  }

  // Handle multi-select answer change
  const handleMultiSelectChange = (questionIndex: number, value: string, checked: boolean) => {
    const currentAnswers = (answers[questionIndex] as string[]) || []

    if (value === "__custom__") {
      setCustomSelected({ ...customSelected, [questionIndex]: checked })
      if (!checked) {
        setCustomInputs({ ...customInputs, [questionIndex]: "" })
      } else {
        // Focus the input after state update
        setTimeout(() => {
          const inputRef = customInputRefs.current[questionIndex]
          if (inputRef) {
            inputRef.focus()
          }
        }, 0)
      }
    } else {
      if (checked) {
        setAnswers({ ...answers, [questionIndex]: [...currentAnswers, value] })
      } else {
        setAnswers({
          ...answers,
          [questionIndex]: currentAnswers.filter((v) => v !== value),
        })
      }
    }
  }

  // Handle custom input change
  const handleCustomInputChange = (questionIndex: number, value: string) => {
    setCustomInputs({ ...customInputs, [questionIndex]: value })
  }

  // Format answers for submission
  const formatAnswersForSubmission = (): string => {
    return questions
      .map((question, index) => {
        const answer = answers[index]
        const isMultiSelect = Array.isArray(answer) || (question.multiSelect && customSelected[index])

        if (isMultiSelect) {
          // Multi-select format
          const selectedAnswers = Array.isArray(answer) ? answer : []
          const answerLines = selectedAnswers.map(ans => `- ${ans}`)

          // Add custom answer if present
          if (customSelected[index] && customInputs[index]?.trim()) {
            answerLines.push(`- Custom answer: ${customInputs[index]}`)
          }

          return `${question.question}\nAnswers:\n${answerLines.join("\n")}`
        } else {
          // Single-select format
          const answerText = customSelected[index]
            ? `Custom answer: ${customInputs[index] || ""}`
            : (answer as string || "")

          return `${question.question}\nAnswer: ${answerText}`
        }
      })
      .join("\n\n")
  }

  // Handle submit
  const handleSubmit = () => {
    const formattedAnswers = formatAnswersForSubmission()
    onSubmit(formattedAnswers, messageId)
    onOpenChange(false)
  }

  // Check if option label contains "(Recommended)"
  const isRecommended = (label: string): boolean => {
    return label.toLowerCase().includes("(recommended)")
  }

  // Scroll to a specific question
  const scrollToQuestion = (index: number) => {
    const questionElement = questionRefs.current[index]
    if (questionElement) {
      questionElement.scrollIntoView({
        behavior: "smooth",
        block: "start",
      })
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-3xl max-h-[70vh] flex flex-col">
        <DialogHeader>
          <DialogTitle>Answer Questions</DialogTitle>
          <DialogDescription>
            Please answer all questions below to continue the conversation
          </DialogDescription>
        </DialogHeader>

        {/* Progress Indicator - only show if multiple questions */}
        {questions.length > 1 && (
          <div className="flex gap-2 py-3 border-b">
            {questions.map((_, index) => (
              <div
                key={index}
                className="flex items-center gap-1 cursor-pointer hover:opacity-70 transition-opacity"
                onClick={() => scrollToQuestion(index)}
                title={`Scroll to question ${index + 1}`}
              >
                {isQuestionAnswered(index) ? (
                  <CheckCircle2 className="h-5 w-5 text-green-600 dark:text-green-400" />
                ) : (
                  <Circle className="h-5 w-5 text-gray-300 dark:text-gray-600" />
                )}
              </div>
            ))}
          </div>
        )}

        {/* Questions List */}
        <div className="flex-1 overflow-y-auto space-y-6 py-4">
          {questions.map((question, qIndex) => (
            <div
              key={qIndex}
              ref={(el) => {
                questionRefs.current[qIndex] = el
              }}
              className="space-y-3"
            >
              {/* Question Header */}
              <div className="flex items-center gap-2">
                <h4 className="font-medium text-base">{question.question}</h4>
                <Badge variant="secondary" className="text-xs">
                  {question.header}
                </Badge>
              </div>

              {/* Options */}
              <div className="space-y-2">
                {question.multiSelect ? (
                  // Multi-select (Checkboxes)
                  <div className="space-y-2">
                    {question.options.map((option, oIndex) => {
                      const isSelected = (answers[qIndex] as string[] || []).includes(option.label)
                      const recommended = isRecommended(option.label)
                      const hasAnySelection = ((answers[qIndex] as string[] || []).length > 0) || customSelected[qIndex]

                      return (
                        <div
                          key={oIndex}
                          className={`flex items-start gap-3 p-3 rounded border transition-all cursor-pointer ${
                            isSelected
                              ? "bg-blue-50 dark:bg-blue-950 border-blue-300 dark:border-blue-700 opacity-100"
                              : hasAnySelection
                                ? "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-60 hover:opacity-100"
                                : "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-100 hover:opacity-100"
                          }`}
                          onClick={() => handleMultiSelectChange(qIndex, option.label, !isSelected)}
                        >
                          <Checkbox
                            id={`q${qIndex}-o${oIndex}`}
                            checked={isSelected}
                            className="pointer-events-none"
                          />
                          <div className="flex-1">
                            <div className="font-medium cursor-pointer flex items-center gap-1.5">
                              {recommended && (
                                <Star className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400 fill-blue-600 dark:fill-blue-400" />
                              )}
                              <span className={recommended ? "text-blue-700 dark:text-blue-300" : ""}>
                                {option.label}
                              </span>
                            </div>
                            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                              {option.description}
                            </p>
                          </div>
                        </div>
                      )
                    })}
                    {/* Custom Option for Multi-select */}
                    <div
                      className={`flex items-start gap-3 p-3 rounded border transition-all cursor-pointer ${
                        customSelected[qIndex]
                          ? "bg-blue-50 dark:bg-blue-950 border-blue-300 dark:border-blue-700 opacity-100"
                          : ((answers[qIndex] as string[] || []).length > 0) || customSelected[qIndex]
                            ? "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-60 hover:opacity-100"
                            : "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-100 hover:opacity-100"
                      }`}
                      onClick={() =>
                        handleMultiSelectChange(qIndex, "__custom__", !customSelected[qIndex])
                      }
                    >
                      <Checkbox
                        id={`q${qIndex}-custom`}
                        checked={customSelected[qIndex] || false}
                        className="pointer-events-none"
                      />
                      <div className="flex-1 space-y-2">
                        <div className="font-medium cursor-pointer">
                          Other (enter custom answer)
                        </div>
                        {customSelected[qIndex] && (
                          <Input
                            ref={(el) => {
                              customInputRefs.current[qIndex] = el
                            }}
                            placeholder="Enter your answer..."
                            value={customInputs[qIndex] || ""}
                            onChange={(e) => handleCustomInputChange(qIndex, e.target.value)}
                            onClick={(e) => e.stopPropagation()}
                            className="mt-2"
                          />
                        )}
                      </div>
                    </div>
                  </div>
                ) : (
                  // Single-select (Radio)
                  <RadioGroup
                    value={customSelected[qIndex] ? "__custom__" : (answers[qIndex] as string) || ""}
                    onValueChange={(value) => handleSingleSelectChange(qIndex, value)}
                  >
                    {question.options.map((option, oIndex) => {
                      const isSelected = answers[qIndex] === option.label
                      const recommended = isRecommended(option.label)
                      const hasAnySelection = !!answers[qIndex] || customSelected[qIndex]

                      return (
                        <div
                          key={oIndex}
                          className={`flex items-start gap-3 p-3 rounded border transition-all cursor-pointer ${
                            isSelected
                              ? "bg-blue-50 dark:bg-blue-950 border-blue-300 dark:border-blue-700 opacity-100"
                              : hasAnySelection
                                ? "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-60 hover:opacity-100"
                                : "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-100 hover:opacity-100"
                          }`}
                          onClick={() => handleSingleSelectChange(qIndex, option.label)}
                        >
                          <RadioGroupItem
                            value={option.label}
                            id={`q${qIndex}-o${oIndex}`}
                            className="pointer-events-none"
                          />
                          <div className="flex-1">
                            <div className="font-medium cursor-pointer flex items-center gap-1.5">
                              {recommended && (
                                <Star className="h-3.5 w-3.5 text-blue-600 dark:text-blue-400 fill-blue-600 dark:fill-blue-400" />
                              )}
                              <span className={recommended ? "text-blue-700 dark:text-blue-300" : ""}>
                                {option.label}
                              </span>
                            </div>
                            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
                              {option.description}
                            </p>
                          </div>
                        </div>
                      )
                    })}
                    {/* Custom Option for Single-select */}
                    <div
                      className={`flex items-start gap-3 p-3 rounded border transition-all cursor-pointer ${
                        customSelected[qIndex]
                          ? "bg-blue-50 dark:bg-blue-950 border-blue-300 dark:border-blue-700 opacity-100"
                          : (!!answers[qIndex] || customSelected[qIndex])
                            ? "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-60 hover:opacity-100"
                            : "bg-white dark:bg-slate-900 border-gray-200 dark:border-gray-700 opacity-100 hover:opacity-100"
                      }`}
                      onClick={() => handleSingleSelectChange(qIndex, "__custom__")}
                    >
                      <RadioGroupItem
                        value="__custom__"
                        id={`q${qIndex}-custom`}
                        className="pointer-events-none"
                      />
                      <div className="flex-1 space-y-2">
                        <div className="font-medium cursor-pointer">
                          Other (enter custom answer)
                        </div>
                        {customSelected[qIndex] && (
                          <Input
                            ref={(el) => {
                              customInputRefs.current[qIndex] = el
                            }}
                            placeholder="Enter your answer..."
                            value={customInputs[qIndex] || ""}
                            onChange={(e) => handleCustomInputChange(qIndex, e.target.value)}
                            onClick={(e) => e.stopPropagation()}
                            className="mt-2"
                          />
                        )}
                      </div>
                    </div>
                  </RadioGroup>
                )}
              </div>
            </div>
          ))}
        </div>

        <DialogFooter>
          <Button
            onClick={handleSubmit}
            disabled={!allQuestionsAnswered}
            className="w-full sm:w-auto"
          >
            Send Answers
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
