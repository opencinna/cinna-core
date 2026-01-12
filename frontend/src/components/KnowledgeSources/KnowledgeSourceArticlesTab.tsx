import { useQuery } from "@tanstack/react-query"
import { BookOpen, AlertCircle } from "lucide-react"

import type { AIKnowledgeGitRepoPublic as KnowledgeSourceRead } from "@/client"
import { KnowledgeSourcesService } from "@/client"
import { Badge } from "@/components/ui/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Skeleton } from "@/components/ui/skeleton"

interface KnowledgeSourceArticlesTabProps {
  source: KnowledgeSourceRead
  sourceId: string
}

export function KnowledgeSourceArticlesTab({
  source,
  sourceId,
}: KnowledgeSourceArticlesTabProps) {
  const { data: articles, isLoading: isLoadingArticles } = useQuery({
    queryKey: ["knowledge-articles", sourceId],
    queryFn: () => KnowledgeSourcesService.listKnowledgeArticles({ sourceId }),
    enabled: !!source && source.is_enabled,
  })

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <BookOpen className="h-5 w-5" />
              Knowledge Articles
            </CardTitle>
            <CardDescription>
              Articles extracted from the repository ({source.article_count} total)
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        {!source.is_enabled ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <AlertCircle className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold">Source is disabled</h3>
            <p className="text-sm text-muted-foreground mt-2">
              Enable this source in the Configuration tab to view articles
            </p>
          </div>
        ) : isLoadingArticles ? (
          <div className="space-y-2">
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
            <Skeleton className="h-12 w-full" />
          </div>
        ) : !articles || articles.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 text-center">
            <BookOpen className="h-12 w-12 text-muted-foreground mb-4" />
            <h3 className="text-lg font-semibold">No articles yet</h3>
            <p className="text-sm text-muted-foreground mt-2">
              Click "Refresh Knowledge" in the Configuration tab to extract articles
            </p>
          </div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Title</TableHead>
                <TableHead>Description</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {articles.map((article) => (
                <TableRow key={article.id}>
                  <TableCell className="font-medium">{article.title}</TableCell>
                  <TableCell>
                    <div className="space-y-2">
                      <span className="text-sm text-muted-foreground line-clamp-2">
                        {article.description}
                      </span>
                      <div className="flex flex-wrap gap-1">
                        {article.tags.slice(0, 3).map((tag, idx) => (
                          <Badge key={idx} variant="secondary" className="text-xs">
                            {tag}
                          </Badge>
                        ))}
                        {article.tags.length > 3 && (
                          <Badge variant="secondary" className="text-xs">
                            +{article.tags.length - 3}
                          </Badge>
                        )}
                        {article.features.slice(0, 2).map((feature, idx) => (
                          <Badge key={`f-${idx}`} variant="outline" className="text-xs">
                            {feature}
                          </Badge>
                        ))}
                        {article.features.length > 2 && (
                          <Badge variant="outline" className="text-xs">
                            +{article.features.length - 2}
                          </Badge>
                        )}
                      </div>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}
