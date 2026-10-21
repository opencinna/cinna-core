# cinna-core-ui-developer memory

## Pitfalls
- NEVER `npx biome format --write <directory>`: most legacy files are not biome-formatted, a dir run rewrites dozens of untouched files. Pass explicit file paths only (zsh: `${=F}` to word-split a file list variable).
- Typecheck: whole-tree `npx tsc --noEmit` is ~fast enough; filter out sibling-owned paths with `grep -v`.

## Primitives / modules (user groups P7, 2026-10-19)
- `Common/UserAllowlistPicker` has opt-in `groups={selected,onAdd,onRemove,isAdding,isRemoving}` + `people={false}`; pill X now has Tooltip + "Remove <name>" aria-label.
- `Common/useUserGroupSearch(query,{enabled,minLength})` key ["user-group-search", debounced].
- `utils/principalKinds.ts` PRINCIPAL_KIND_ICON user=User, group=Users, role=ShieldCheck.
- `utils/sharedWith.ts` peopleAndGroups / sharedWithSentence / viaGroupsLabel.
- `Environments/useSharedAICredentials` + composeEnvModeConfigFields takes {id,type}[] (owned + shared concat in AddEnvironment, EnvironmentCard, routes/_layout/index.tsx).
- CatalogPrincipalPicker onPick(principal, title, {email?, memberCount?}).
