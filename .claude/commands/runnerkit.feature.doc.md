---
description: Create or update documentation file following project practices.
---

## User Input

```text
$ARGUMENTS
```

Feature name or description to document. If updating existing doc, provide path.

## Task

Create/update feature documentation in `docs/` following the reference-based style.

## Documentation Style

**Core principles:**
- NO code blocks - only file/method references
- Explain business logic and flows, not implementation details
- Heavy use of file paths: `backend/app/services/file_service.py:method_name()`
- Concise bullet points over paragraphs

## Required Sections

1. **Purpose** - One sentence explaining what the feature does

2. **Feature Overview** - Brief flow description (numbered steps)

3. **Architecture** - Simple text diagram showing component flow
   ```
   Frontend → Backend API → Storage → External Service
   ```

4. **Data/State Lifecycle** - States, transitions, business rules

5. **Database Schema** (if applicable)
   - Migration file path
   - Model file paths
   - Table names and key fields (no schema code)

6. **Backend Implementation**
   - Routes: file paths + endpoint signatures
   - Services: file paths + key method names
   - Configuration: settings file + relevant config keys

7. **Frontend Implementation**
   - Components: file paths + purpose
   - Hooks: file paths + what they manage
   - Routes: file paths for page routes

8. **Security Features** - Validation rules, access control logic

9. **Key Integration Points** - How components connect, data flows

10. **File Locations Reference** - Comprehensive list grouped by layer

11. **Footer** - Document version, date, status

## Example References

Good:
- `backend/app/services/file_service.py:upload_files_to_agent_env()`
- `POST /api/v1/files/upload` - Upload file (creates temporary record)
- **FileUploadModal:** `frontend/src/components/Chat/FileUploadModal.tsx`

Bad:
- Including actual code snippets
- Explaining how to write the code
- Tutorial-style instructions

## Reference

See `docs/file-management/file_management_overview.md` for exemplary documentation.

## Output

Write documentation to `docs/{feature-name}/{feature}_overview.md`
