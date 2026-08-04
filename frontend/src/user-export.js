export function userExportFilename(contentDisposition = '') {
  const encoded = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1])
    } catch {
      // Fall through to the plain filename when the encoded value is invalid.
    }
  }
  return contentDisposition.match(/filename="?([^";]+)"?/i)?.[1] || 'users.csv'
}

export async function downloadAllUsersCsv(
  apiClient,
  { documentRef = document, urlApi = URL } = {},
) {
  const response = await apiClient.get('/admin/users/export', { responseType: 'blob' })
  const objectUrl = urlApi.createObjectURL(response.data)
  const anchor = documentRef.createElement('a')
  anchor.href = objectUrl
  anchor.download = userExportFilename(response.headers?.['content-disposition'] || '')
  documentRef.body.appendChild(anchor)
  try {
    anchor.click()
    return anchor.download
  } finally {
    anchor.remove()
    urlApi.revokeObjectURL(objectUrl)
  }
}
