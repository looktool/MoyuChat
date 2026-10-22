// 与 Python 服务器（common.py / server.py）协议对应的前端类型定义。
// 网络层以 JSON 文本帧传输；ws_bridge.py 负责与后端的「4 字节长度前缀」TCP 帧互转。

export type ConnStatus = 'disconnected' | 'connecting' | 'online' | 'kicked'

export type ConvType = 'user' | 'group'

export interface Message {
  // 消息 id：本地/旧数据为自增整数，联邦跨服务器后改为全局唯一 UUID 字符串
  id?: number | string
  conv: string // 本端会话名：私聊=对方昵称；群聊=群名
  from: string
  to?: string // 私聊目标（仅私聊）
  group?: string // 群聊名（仅群聊）
  text: string
  ts: number
  system?: boolean
  recalled?: boolean
  status?: 'sent' | 'read' // 仅己方发出的消息有：已送达 / 已读
  replay?: boolean // 登录回放的历史，不计未读
}

export interface Conversation {
  name: string
  type: ConvType
  last?: Message
  unread: number
  pinned: boolean
  presence?: boolean // 私聊对方是否在线
  members?: string[] // 群成员（已加入的群才有）
}

export interface GroupInfo {
  name: string
  member_count: number
  joined: boolean
}

// 来自服务器的消息（宽松类型，便于对接）
export type ServerMsg = Record<string, any>
export type ClientMsg = Record<string, any>
