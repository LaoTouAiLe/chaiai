import { create } from 'zustand'

export interface PendingAttachment {
  id: string
  previewUrl?: string
  name: string
  kind: 'image' | 'file'
  sizeBytes?: number
}

interface AttachmentDraft {
  items: readonly PendingAttachment[]
  uploading: string | null
  sending: boolean
  error: string | null
}

export const EMPTY_ATTACHMENT_DRAFT: AttachmentDraft = {
  items: [], uploading: null, sending: false, error: null,
}

/** 附件与文字草稿一样按会话保留；异步回调始终写回发起操作的会话。 */
export const useAttachmentDrafts = create<{
  drafts: Record<string, AttachmentDraft>
  update: (scope: string, change: (draft: AttachmentDraft) => AttachmentDraft) => void
}>((set) => ({
  drafts: {},
  update: (scope, change) => set(state => ({
    drafts: { ...state.drafts, [scope]: change(state.drafts[scope] ?? EMPTY_ATTACHMENT_DRAFT) },
  })),
}))
