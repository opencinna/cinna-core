export type ColorPreset =
  | "slate"
  | "blue"
  | "indigo"
  | "purple"
  | "pink"
  | "rose"
  | "orange"
  | "amber"
  | "emerald"
  | "teal"
  | "cyan"
  | "sky"

export interface ColorPresetConfig {
  name: string
  value: ColorPreset
  // For AgentCard icon
  iconBg: string
  iconText: string
  // For badge background
  badgeBg: string
  // For badge text
  badgeText: string
  // For badge outline (when selected)
  badgeOutline: string
  // For badge hover
  badgeHover: string
}

export const COLOR_PRESETS: ColorPresetConfig[] = [
  {
    name: "Slate",
    value: "slate",
    iconBg: "bg-slate-100 dark:bg-slate-800",
    iconText: "text-slate-600 dark:text-slate-300",
    badgeBg: "bg-slate-100 dark:bg-slate-800",
    badgeText: "text-slate-700 dark:text-slate-200",
    badgeOutline: "ring-2 ring-slate-400 dark:ring-slate-500",
    badgeHover: "hover:bg-slate-200/80 dark:hover:bg-slate-700/80",
  },
  {
    name: "Blue",
    value: "blue",
    iconBg: "bg-blue-100 dark:bg-blue-900/40",
    iconText: "text-blue-600 dark:text-blue-400",
    badgeBg: "bg-blue-100 dark:bg-blue-900/40",
    badgeText: "text-blue-700 dark:text-blue-300",
    badgeOutline: "ring-2 ring-blue-400 dark:ring-blue-500",
    badgeHover: "hover:bg-blue-200/80 dark:hover:bg-blue-800/60",
  },
  {
    name: "Indigo",
    value: "indigo",
    iconBg: "bg-indigo-100 dark:bg-indigo-900/40",
    iconText: "text-indigo-600 dark:text-indigo-400",
    badgeBg: "bg-indigo-100 dark:bg-indigo-900/40",
    badgeText: "text-indigo-700 dark:text-indigo-300",
    badgeOutline: "ring-2 ring-indigo-400 dark:ring-indigo-500",
    badgeHover: "hover:bg-indigo-200/80 dark:hover:bg-indigo-800/60",
  },
  {
    name: "Purple",
    value: "purple",
    iconBg: "bg-purple-100 dark:bg-purple-900/40",
    iconText: "text-purple-600 dark:text-purple-400",
    badgeBg: "bg-purple-100 dark:bg-purple-900/40",
    badgeText: "text-purple-700 dark:text-purple-300",
    badgeOutline: "ring-2 ring-purple-400 dark:ring-purple-500",
    badgeHover: "hover:bg-purple-200/80 dark:hover:bg-purple-800/60",
  },
  {
    name: "Pink",
    value: "pink",
    iconBg: "bg-pink-100 dark:bg-pink-900/40",
    iconText: "text-pink-600 dark:text-pink-400",
    badgeBg: "bg-pink-100 dark:bg-pink-900/40",
    badgeText: "text-pink-700 dark:text-pink-300",
    badgeOutline: "ring-2 ring-pink-400 dark:ring-pink-500",
    badgeHover: "hover:bg-pink-200/80 dark:hover:bg-pink-800/60",
  },
  {
    name: "Rose",
    value: "rose",
    iconBg: "bg-rose-100 dark:bg-rose-900/40",
    iconText: "text-rose-600 dark:text-rose-400",
    badgeBg: "bg-rose-100 dark:bg-rose-900/40",
    badgeText: "text-rose-700 dark:text-rose-300",
    badgeOutline: "ring-2 ring-rose-400 dark:ring-rose-500",
    badgeHover: "hover:bg-rose-200/80 dark:hover:bg-rose-800/60",
  },
  {
    name: "Orange",
    value: "orange",
    iconBg: "bg-orange-100 dark:bg-orange-900/40",
    iconText: "text-orange-600 dark:text-orange-400",
    badgeBg: "bg-orange-100 dark:bg-orange-900/40",
    badgeText: "text-orange-700 dark:text-orange-300",
    badgeOutline: "ring-2 ring-orange-400 dark:ring-orange-500",
    badgeHover: "hover:bg-orange-200/80 dark:hover:bg-orange-800/60",
  },
  {
    name: "Amber",
    value: "amber",
    iconBg: "bg-amber-100 dark:bg-amber-900/40",
    iconText: "text-amber-600 dark:text-amber-400",
    badgeBg: "bg-amber-100 dark:bg-amber-900/40",
    badgeText: "text-amber-700 dark:text-amber-300",
    badgeOutline: "ring-2 ring-amber-400 dark:ring-amber-500",
    badgeHover: "hover:bg-amber-200/80 dark:hover:bg-amber-800/60",
  },
  {
    name: "Emerald",
    value: "emerald",
    iconBg: "bg-emerald-100 dark:bg-emerald-900/40",
    iconText: "text-emerald-600 dark:text-emerald-400",
    badgeBg: "bg-emerald-100 dark:bg-emerald-900/40",
    badgeText: "text-emerald-700 dark:text-emerald-300",
    badgeOutline: "ring-2 ring-emerald-400 dark:ring-emerald-500",
    badgeHover: "hover:bg-emerald-200/80 dark:hover:bg-emerald-800/60",
  },
  {
    name: "Teal",
    value: "teal",
    iconBg: "bg-teal-100 dark:bg-teal-900/40",
    iconText: "text-teal-600 dark:text-teal-400",
    badgeBg: "bg-teal-100 dark:bg-teal-900/40",
    badgeText: "text-teal-700 dark:text-teal-300",
    badgeOutline: "ring-2 ring-teal-400 dark:ring-teal-500",
    badgeHover: "hover:bg-teal-200/80 dark:hover:bg-teal-800/60",
  },
  {
    name: "Cyan",
    value: "cyan",
    iconBg: "bg-cyan-100 dark:bg-cyan-900/40",
    iconText: "text-cyan-600 dark:text-cyan-400",
    badgeBg: "bg-cyan-100 dark:bg-cyan-900/40",
    badgeText: "text-cyan-700 dark:text-cyan-300",
    badgeOutline: "ring-2 ring-cyan-400 dark:ring-cyan-500",
    badgeHover: "hover:bg-cyan-200/80 dark:hover:bg-cyan-800/60",
  },
  {
    name: "Sky",
    value: "sky",
    iconBg: "bg-sky-100 dark:bg-sky-900/40",
    iconText: "text-sky-600 dark:text-sky-400",
    badgeBg: "bg-sky-100 dark:bg-sky-900/40",
    badgeText: "text-sky-700 dark:text-sky-300",
    badgeOutline: "ring-2 ring-sky-400 dark:ring-sky-500",
    badgeHover: "hover:bg-sky-200/80 dark:hover:bg-sky-800/60",
  },
]

export const getColorPreset = (
  preset: string | null | undefined
): ColorPresetConfig => {
  const found = COLOR_PRESETS.find((p) => p.value === preset)
  return found || COLOR_PRESETS[0] // Default to slate
}
