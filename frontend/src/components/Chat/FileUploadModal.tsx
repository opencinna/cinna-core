import { useCallback, useState } from "react"
import { useDropzone } from 'react-dropzone'
import { useMutation } from "@tanstack/react-query"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Upload, FileCheck, AlertCircle, Loader2 } from "lucide-react"
import { FilesService } from "@/client"
import type { FileUploadPublic } from "@/client"

interface FileUploadModalProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  onFileUploaded: (file: FileUploadPublic) => void
}

export function FileUploadModal({ open, onOpenChange, onFileUploaded }: FileUploadModalProps) {
  const [error, setError] = useState<string | null>(null)

  const uploadFile = useMutation({
    mutationFn: (file: File) => {
      return FilesService.uploadFile({ formData: { file } })
    },
    onSuccess: (data) => {
      onFileUploaded(data)
      setError(null)
    },
    onError: (err: any) => {
      setError(err.response?.data?.detail || "Upload failed")
    }
  })

  const onDrop = useCallback((acceptedFiles: File[]) => {
    setError(null)
    acceptedFiles.forEach(file => {
      // Validate file size (100MB)
      if (file.size > 100 * 1024 * 1024) {
        setError(`File ${file.name} is too large (max 100MB)`)
        return
      }

      uploadFile.mutate(file)
    })
  }, [uploadFile])

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    multiple: true,
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Upload Files</DialogTitle>
        </DialogHeader>

        <div
          {...getRootProps()}
          className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors ${
            isDragActive ? 'border-primary bg-primary/5' : 'border-muted-foreground/25'
          }`}
        >
          <input {...getInputProps()} />
          <Upload className="h-12 w-12 mx-auto mb-4 text-muted-foreground" />
          {isDragActive ? (
            <p className="text-sm text-muted-foreground">Drop files here...</p>
          ) : (
            <>
              <p className="text-sm font-medium mb-1">Drag & drop files here</p>
              <p className="text-xs text-muted-foreground">or click to browse</p>
              <p className="text-xs text-muted-foreground mt-2">Max 100MB per file</p>
            </>
          )}
        </div>

        {uploadFile.isPending && (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            Uploading...
          </div>
        )}

        {error && (
          <div className="flex items-center gap-2 text-sm text-destructive">
            <AlertCircle className="h-4 w-4" />
            {error}
          </div>
        )}

        {uploadFile.isSuccess && (
          <div className="flex items-center gap-2 text-sm text-green-600 dark:text-green-400">
            <FileCheck className="h-4 w-4" />
            File uploaded successfully
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
