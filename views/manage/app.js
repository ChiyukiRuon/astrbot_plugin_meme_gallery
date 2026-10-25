const bridge = window.AstrBotPluginView
const $ = id => document.getElementById(id)
const state = { galleries: [], galleryId: null, page: 1, total: 0, selected: new Set(), rows: [], generation: 0, busy: false, importing: false, stop: false, files: [], maxBytes: 20 * 1024 * 1024 }
let submitEditor = null
let fieldId = 0
let originalRequest = 0

async function openOriginal(row) {
    const requestId = ++originalRequest
    const image = $('original-image')
    image.hidden = true
    image.removeAttribute('src')
    image.alt = `${row.gallery} · ID ${row.id} 原图`
    $('original-title').textContent = `查看原图 · ID ${row.id}`
    $('original-status').textContent = '正在加载原图…'
    $('original-stage').classList.remove('actual-size')
    $('original-size').textContent = '原始尺寸'
    $('original-size').disabled = true
    $('original-dialog').showModal()
    try {
        const result = await bridge.apiGet(`images/${row.id}/original`)
        if (requestId !== originalRequest || !$('original-dialog').open) return
        image.onload = () => {
            if (requestId !== originalRequest) return
            $('original-status').textContent = `${result.width} × ${result.height}${result.animated ? ' · 动图' : ''}`
            $('original-size').disabled = false
        }
        image.onerror = () => {
            if (requestId !== originalRequest) return
            image.hidden = true
            $('original-status').textContent = '原图无法显示，文件可能损坏或浏览器不支持该格式。'
        }
        image.src = result.src
        image.hidden = false
    } catch (error) {
        if (requestId === originalRequest) $('original-status').textContent = `加载失败：${error.message}`
    }
}
$('close-original').addEventListener('click', () => $('original-dialog').close())
$('original-dialog').addEventListener('close', () => {
    originalRequest++
    const image = $('original-image')
    image.onload = null
    image.onerror = null
    image.removeAttribute('src')
    image.hidden = true
})
$('original-size').addEventListener('click', () => {
    const actual = $('original-stage').classList.toggle('actual-size')
    $('original-size').textContent = actual ? '适应窗口' : '原始尺寸'
})

function notice(message) { $('notice').textContent = message }
function element(tag, text, className) {
    const node = document.createElement(tag)
    if (text !== undefined) node.textContent = text
    if (className) node.className = className
    return node
}
function button(text, callback) {
    const node = element('button', text)
    node.type = 'button'
    node.addEventListener('click', () => action(callback))
    return node
}
async function action(callback) {
    if (state.busy || state.importing) return
    state.busy = true
    document.body.dataset.busy = 'true'
    document.body.setAttribute('aria-busy', 'true')
    try { await callback() } catch (error) {
        if ($('editor').open) $('editor-error').textContent = error.message
        else notice(error.message)
    } finally {
        state.busy = false
        document.body.dataset.busy = 'false'
        document.body.setAttribute('aria-busy', 'false')
    }
}
function currentGallery() { return state.galleries.find(item => item.id === state.galleryId) }
function updateSelection() {
    $('selected-count').textContent = `已选 ${state.selected.size} 张`
    $('delete-selected').disabled = state.selected.size === 0
    $('select-all').checked = state.rows.length > 0 && state.selected.size === state.rows.length
    $('select-all').indeterminate = state.selected.size > 0 && state.selected.size < state.rows.length
}
function renderGalleries() {
    const query = $('gallery-search').value.trim().toLocaleLowerCase()
    const list = $('galleries')
    list.replaceChildren()
    const entries = [{ id: null, name: '全部图片', count: state.galleries.reduce((sum, g) => sum + g.count, 0), aliases: [] }, ...state.galleries]
    for (const gallery of entries) {
        if (gallery.id !== null && ![gallery.name, ...gallery.aliases].some(name => name.toLocaleLowerCase().includes(query))) continue
        const item = button('', async () => {
            state.galleryId = gallery.id
            state.page = 1
            $('image-search').value = ''
            renderGalleries()
            await loadImages()
        })
        item.className = 'gallery-link'
        item.setAttribute('aria-current', String(gallery.id === state.galleryId))
        item.append(element('span', gallery.name), element('span', String(gallery.count), 'muted'))
        list.append(item)
    }
    const gallery = currentGallery()
    $('gallery-title').textContent = gallery?.name ?? '全部图片'
    $('aliases').textContent = gallery ? (gallery.aliases.length ? `别名：${gallery.aliases.join('、')}` : '暂无别名') : '浏览所有图库，或选择一个图库开始整理。'
    $('edit-gallery').hidden = !gallery
    $('import').disabled = !gallery
}
async function refresh() {
    const result = await bridge.apiGet('galleries')
    state.galleries = result.items
    state.maxBytes = result.max_upload_bytes
    if (!currentGallery()) state.galleryId = null
    renderGalleries()
    await loadImages()
}
async function loadImages() {
    const generation = ++state.generation
    $('images').setAttribute('aria-busy', 'true')
    notice('正在加载图片…')
    try {
        const params = { page: state.page, q: $('image-search').value.trim() }
        if (state.galleryId !== null) params.gallery_id = state.galleryId
        let result = await bridge.apiGet('images', params)
        const lastPage = Math.max(1, Math.ceil(result.total / 20))
        if (state.page > lastPage) {
            state.page = lastPage
            result = await bridge.apiGet('images', { ...params, page: lastPage })
        }
        if (generation !== state.generation) return
        state.rows = result.items
        state.total = result.total
        state.selected.clear()
        updateSelection()
        $('images').replaceChildren()
        $('empty').hidden = result.items.length !== 0
        $('empty').querySelector('h3').textContent = params.q ? '没有找到这张图片' : '这里还没有图片'
        $('empty').querySelector('p').textContent = params.q ? '请检查图片 ID，或切换到全部图片查找。' : '选择或新建一个图库，然后导入你的收藏。'
        $('total').textContent = `共 ${result.total} 张`
        $('page').textContent = `第 ${state.page} / ${lastPage} 页`
        $('previous').disabled = state.page <= 1
        $('next').disabled = state.page >= lastPage
        const previews = []
        for (const row of result.items) {
            const card = element('article', undefined, 'image-card')
            const thumb = button('加载预览…', () => { void openOriginal(row) })
            thumb.className = 'thumbnail'
            thumb.setAttribute('aria-label', `查看原图 ID ${row.id}`)
            thumb.title = '点击查看原图'
            const info = element('div', undefined, 'card-info')
            const label = element('label')
            const checkbox = document.createElement('input')
            checkbox.type = 'checkbox'
            checkbox.dataset.id = row.id
            checkbox.addEventListener('change', () => {
                if (checkbox.checked) state.selected.add(row.id)
                else state.selected.delete(row.id)
                updateSelection()
            })
            label.append(checkbox, document.createTextNode(`ID ${row.id}`))
            info.append(label, element('p', row.gallery, 'muted'), button('编辑图片', () => editImage(row)))
            card.append(thumb, info)
            $('images').append(card)
            previews.push({ row, thumb })
        }
        notice('已加载。点击图片查看原图，动图可在原图窗口播放。')
        async function previewWorker() {
            while (previews.length && generation === state.generation) {
                const { row, thumb } = previews.shift()
                try {
                    const result = await bridge.apiGet(`images/${row.id}/preview`)
                    if (generation !== state.generation) return
                    const image = document.createElement('img')
                    image.alt = `${row.gallery} · ID ${row.id}${result.animated ? ' · 动图首帧' : ''}`
                    image.src = result.src
                    thumb.replaceChildren(image)
                    if (result.animated) {
                        const badge = element('span', 'GIF', 'gif-badge')
                        badge.title = '动图，点击查看原图播放'
                        thumb.append(badge)
                    }
                } catch { thumb.textContent = '预览加载失败，可刷新重试' }
            }
        }
        void Promise.all([previewWorker(), previewWorker()])
    } finally { $('images').setAttribute('aria-busy', 'false') }
}
function openEditor(title, content, submit, label = '保存') {
    $('editor-title').textContent = title
    $('editor-content').replaceChildren(...content)
    $('editor-error').textContent = ''
    $('editor-submit').textContent = label
    $('editor-submit').hidden = !submit
    submitEditor = submit
    if (!$('editor').open) $('editor').showModal()
}
function field(title, input) {
    const group = element('div', undefined, 'field')
    const label = element('label', title)
    input.id = `editor-field-${++fieldId}`
    label.htmlFor = input.id
    group.append(label, input)
    return group
}
function nameInput(value = '') {
    const input = document.createElement('input')
    input.value = value
    input.maxLength = 80
    input.required = true
    return input
}
function confirmAction(title, message, callback) {
    openEditor(title, [element('p', message)], async () => {
        await callback()
        $('editor').close()
        await refresh()
    }, '确认删除')
}
function editGallery() {
    const gallery = currentGallery()
    const name = nameInput(gallery.name)
    const aliases = element('div', undefined, 'alias-list')
    aliases.append(element('h3', '别名'))
    for (const alias of gallery.aliases) {
        const row = element('div', undefined, 'alias-row')
        row.append(element('span', alias), button('移除', async () => {
            await bridge.apiPost('galleries/save', { action: 'remove_alias', id: gallery.id, name: alias })
            await refresh()
            editGallery()
        }))
        aliases.append(row)
    }
    const add = button('添加别名', () => {
        const input = nameInput()
        openEditor('添加图库别名', [field('别名', input)], async () => {
            await bridge.apiPost('galleries/save', { action: 'add_alias', id: gallery.id, name: input.value })
            await refresh()
            editGallery()
        })
    })
    const remove = button('删除整个图库', () => confirmAction('删除图库', `确定删除“${gallery.name}”及其中 ${gallery.count} 张图片和所有别名？此操作无法撤销。`, async () => {
        await bridge.apiPost('galleries/delete', { id: gallery.id })
        state.galleryId = null
    }))
    openEditor('管理图库', [field('图库名称', name), aliases, add, element('hr'), remove], async () => {
        if (name.value.trim() !== gallery.name) await bridge.apiPost('galleries/save', { action: 'rename', id: gallery.id, name: name.value })
        $('editor').close()
        await refresh()
    })
}
function editImage(row) {
    const target = document.createElement('select')
    for (const gallery of state.galleries) {
        const option = element('option', gallery.name)
        option.value = gallery.id
        option.selected = gallery.name === row.gallery
        target.append(option)
    }
    const file = document.createElement('input')
    file.type = 'file'
    file.accept = 'image/*'
    const replace = button('替换原图', async () => {
        if (!file.files.length) throw new Error('请先选择替换图片。')
        if (file.files[0].size > state.maxBytes) throw new Error('图片超过大小限制。')
        await bridge.upload(`images/${row.id}/replace`, file.files[0])
        $('editor').close()
        await refresh()
        notice(`已替换图片 ID ${row.id}，ID 保持不变。`)
    })
    const remove = button('删除这张图片', () => confirmAction('删除图片', `确定删除图片 ID ${row.id} 的记录及原文件？此操作无法撤销。`, async () => {
        const result = await bridge.apiPost('images/delete', { ids: [row.id] })
        if (result.errors.length) throw new Error(result.errors.join('\n'))
    }))
    openEditor(`编辑图片 · ID ${row.id}`, [field('所属图库', target), element('p', '移动或替换都保留图片 ID。替换会覆盖原图，无法撤销。', 'muted'), field('选择替换图片', file), replace, element('hr'), remove], async () => {
        await bridge.apiPost('images/move', { id: row.id, gallery_id: Number(target.value) })
        $('editor').close()
        await refresh()
    }, '保存所属图库')
}
function openImport() {
    const gallery = currentGallery()
    state.files = []
    state.importGallery = { ...gallery }
    $('import-target').textContent = `导入到“${gallery.name}” · 单张最多 ${Math.floor(state.maxBytes / 1024 / 1024)} MB`
    $('files').value = ''
    $('folder').value = ''
    $('import-results').replaceChildren()
    $('queue-count').textContent = '尚未选择文件'
    $('import-summary').textContent = ''
    $('progress').value = 0
    $('start-import').disabled = true
    $('import-dialog').showModal()
}
async function startImport() {
    if (state.importing || !state.files.length) return
    state.importing = true
    state.stop = false
    const files = [...state.files]
    const target = state.importGallery.id
    const counts = { added: 0, duplicate: 0, failed: 0 }
    $('start-import').disabled = true
    $('files').disabled = true
    $('folder').disabled = true
    $('close-import').disabled = true
    $('cancel-import').hidden = false
    $('progress').max = files.length
    $('progress').value = 0
    $('import-results').replaceChildren()
    try {
        for (const file of files) {
            if (state.stop) break
            let resultText
            try {
                if (file.size > state.maxBytes) throw new Error('超过单张大小限制')
                const result = await bridge.upload(`galleries/${target}/import`, file)
                counts[result.added ? 'added' : 'duplicate']++
                resultText = `${result.added ? '已导入' : '重复，已跳过'} · ID ${result.id}`
            } catch (error) { counts.failed++; resultText = `失败：${error.message}` }
            $('import-results').append(element('li', `${file.webkitRelativePath || file.name} — ${resultText}`))
            $('progress').value++
            $('import-summary').textContent = `已处理 ${$('progress').value}/${files.length}：成功 ${counts.added}，重复 ${counts.duplicate}，失败 ${counts.failed}`
        }
        if (state.stop) $('import-summary').textContent += '。已停止，完成的导入已保留。'
        else $('import-summary').textContent += '。导入完成。'
        state.files = []
        await refresh()
    } catch (error) { notice(error.message) } finally {
        state.importing = false
        $('files').disabled = false
        $('folder').disabled = false
        $('close-import').disabled = false
        $('cancel-import').hidden = true
    }
}
$('editor-form').addEventListener('submit', event => {
    event.preventDefault()
    action(async () => {
        $('editor-submit').disabled = true
        try { if (submitEditor) await submitEditor() } finally { $('editor-submit').disabled = false }
    })
})
$('close-editor').addEventListener('click', () => { if (!state.busy) $('editor').close() })
$('editor').addEventListener('cancel', event => { if (state.busy) event.preventDefault() })
$('refresh').addEventListener('click', () => action(refresh))
$('gallery-search').addEventListener('input', renderGalleries)
$('create').addEventListener('click', () => action(() => {
    const input = nameInput()
    openEditor('新建图库', [field('图库名称', input)], async () => {
        const result = await bridge.apiPost('galleries/save', { name: input.value })
        if (!result.created) throw new Error('这个图库已经存在。')
        await refresh()
        state.galleryId = state.galleries.find(g => g.name === input.value.trim())?.id ?? null
        state.page = 1
        renderGalleries()
        await loadImages()
        $('editor').close()
    }, '创建图库')
}))
$('edit-gallery').addEventListener('click', () => action(editGallery))
$('search-form').addEventListener('submit', event => {
    event.preventDefault()
    action(async () => { state.page = 1; await loadImages() })
})
$('previous').addEventListener('click', () => action(async () => { state.page--; await loadImages() }))
$('next').addEventListener('click', () => action(async () => { state.page++; await loadImages() }))
$('select-all').addEventListener('change', event => {
    state.selected = new Set(event.target.checked ? state.rows.map(row => row.id) : [])
    for (const input of $('images').querySelectorAll('input[type="checkbox"]')) input.checked = state.selected.has(Number(input.dataset.id))
    updateSelection()
})
$('delete-selected').addEventListener('click', () => action(() => {
    const ids = [...state.selected]
    confirmAction('批量删除图片', `确定删除所选 ${ids.length} 张图片的记录及原文件？此操作无法撤销。`, async () => {
        const result = await bridge.apiPost('images/delete', { ids })
        if (result.errors.length) {
            await refresh()
            throw new Error(`已删除 ${result.deleted.length} 张。${result.errors.join(' ')}`)
        }
    })
}))
$('import').addEventListener('click', () => action(openImport))
for (const id of ['files', 'folder']) $(id).addEventListener('change', event => {
    state.files = [...event.target.files]
    $('queue-count').textContent = `已选择 ${state.files.length} 个文件`
    $('start-import').disabled = !state.files.length
})
$('start-import').addEventListener('click', startImport)
$('cancel-import').addEventListener('click', () => { state.stop = true })
$('close-import').addEventListener('click', () => { if (!state.importing) $('import-dialog').close() })
$('import-dialog').addEventListener('cancel', event => { if (state.importing) event.preventDefault() })
try {
    if (!bridge) throw new Error('请从 AstrBot WebUI 的插件详情页打开“图库管理”。需要支持插件可视化视图的 AstrBot。')
    const context = await bridge.ready()
    const theme = context => { document.documentElement.dataset.dark = String(Boolean(context.isDark)) }
    theme(context)
    const off = bridge.onContext(theme)
    window.addEventListener('beforeunload', off)
    await action(refresh)
} catch (error) { notice(error.message) }
