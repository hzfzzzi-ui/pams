import axios from 'axios'
import type { AxiosInstance, AxiosRequestConfig } from 'axios'

// 创建 axios 实例
const apiClient: AxiosInstance = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    return config
  },
  (error) => {
    console.error('Request error:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    return response.data
  },
  (error) => {
    console.error('Response error:', error)

    if (error.response) {
      // 服务器返回错误状态码
      const { status, data } = error.response

      switch (status) {
        case 404:
          console.error('API endpoint not found:', error.config.url)
          break
        case 500:
          console.error('Server error:', data)
          break
        default:
          console.error(`Error ${status}:`, data)
      }
    } else if (error.request) {
      // 请求已发送但未收到响应
      console.error('No response received:', error.request)
    } else {
      // 请求配置错误
      console.error('Request setup error:', error.message)
    }

    return Promise.reject(error)
  }
)

// API 方法封装
export const api = {
  get<T = any>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return apiClient.get(url, config)
  },

  post<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    return apiClient.post(url, data, config)
  },

  put<T = any>(url: string, data?: any, config?: AxiosRequestConfig): Promise<T> {
    return apiClient.put(url, data, config)
  },

  delete<T = any>(url: string, config?: AxiosRequestConfig): Promise<T> {
    return apiClient.delete(url, config)
  }
}

export default api
