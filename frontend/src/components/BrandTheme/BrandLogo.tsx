import { useState } from "react"

interface BrandLogoProps {
  src: string
  alt: string
  /** Rendered instead when the asset fails to load. */
  fallback: React.ReactNode
}

/**
 * The brand's logo in the 240 × 62 header box, scaled to fit. The box is not
 * enlarged for retina on purpose: the validator's raster minimum (124 px tall)
 * is exactly 2 DPR at 62. No drop shadow — the brand's asset is not ours to
 * decorate.
 */
export function BrandLogo({ src, alt, fallback }: BrandLogoProps) {
  // Keyed by URL so switching mode retries the other mode's asset.
  const [failedSrc, setFailedSrc] = useState<string | null>(null)
  if (failedSrc === src) return <>{fallback}</>
  return (
    <img
      src={src}
      alt={alt}
      className="relative h-[62px] w-[240px] object-contain"
      onError={() => setFailedSrc(src)}
    />
  )
}
