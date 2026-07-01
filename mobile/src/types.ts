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
  original?: string;
  attachment?: Attachment;
}

export interface MessagesResponse {
  ok: boolean;
  platform: string;
  id: number;
  name: string;
  product: string;
  target_lang: string;
  messages: Message[];
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
