export interface Avatar {
  kind: "brand" | "generic" | "initial";
  logo?: string;
  background?: string;
  mark?: string;
  name?: string;
  label?: string;
  initial?: string;
}

export interface Conversation {
  platform: string;
  id: number;
  name: string;
  email: string;
  preview: string;
  product: string;
  time: string;
  time_label: string;
  unread: number;
  initial: string;
  avatar?: Avatar;
}

export interface ConversationsResponse {
  ok: boolean;
  conversations: Conversation[];
  unread_total: number;
  errors: string[];
  error?: string;
}

export interface Attachment {
  filename: string;
  url: string;
  preview: string;
  is_image: boolean;
}

export interface Message {
  id: string;
  direction: "in" | "out";
  author: string;
  date: string;
  text: string;
  translate: boolean;
  translated: string;
  lang: string;
  attachment?: Attachment;
}

export interface OrderOption {
  name: string;
  value: string;
}

export interface PhraseFile {
  filename: string;
  url: string;
  is_image: boolean;
}

export interface CommonPhrase {
  id: string;
  text: string;
  files: PhraseFile[];
}

export interface PhrasesResponse {
  ok: boolean;
  phrases: CommonPhrase[];
  error?: string;
}

export interface PhraseSaveResponse {
  ok: boolean;
  id?: string;
  error?: string;
}

export interface PhraseDeleteResponse {
  ok: boolean;
  error?: string;
}

export interface VerifyStatus {
  needs: boolean;
  state?: string;
  verified?: boolean;
}

export interface DeliveryStatus {
  supported: boolean;
  status?: string;
  delivered?: boolean;
}

export interface MessagesResponse {
  ok: boolean;
  platform: string;
  id: number;
  name: string;
  product: string;
  target_lang: string;
  messages: Message[];
  options?: OrderOption[];
  verify?: VerifyStatus;
  delivery?: DeliveryStatus;
  error?: string;
}

export interface DeliverResponse {
  ok: boolean;
  platform?: string;
  id?: number;
  status?: string;
  delivered?: boolean;
  error?: string;
}

export interface VerifyCodeItem {
  code: string;
  invoice?: number | string;
  product_name?: string;
  product_id?: number | string;
  amount?: number | string;
  currency?: string;
  date_pay?: string;
  email?: string;
  state?: number | string;
  state_label?: string;
}

export interface VerifyCodeResponse {
  ok: boolean;
  item?: VerifyCodeItem;
  error?: string;
}

export interface TranslateResult {
  id: string;
  text: string;
  translated: string;
  source_lang: string;
  label: string;
}

export interface TranslateResponse {
  ok: boolean;
  results: TranslateResult[];
  error?: string;
}

export interface SendResponse {
  ok: boolean;
  platform: string;
  id: number;
  sent_text: string;
  error?: string;
}
