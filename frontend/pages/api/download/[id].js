export default async function handler(req, res) {
    if (req.method !== 'GET') {
        return res.status(405).json({ error: 'Method not allowed' });
    }

    const { id } = req.query;

    if (!id) {
        return res.status(400).json({ error: 'Share ID is required' });
    }

    try {
        // バックエンドAPIを呼び出してファイルをダウンロード
        const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8001';
        const response = await fetch(`${backendUrl}/share/${id}/download`, {
            method: 'GET',
        });

        if (!response.ok) {
            // エラーレスポンスの詳細を取得
            const errorText = await response.text();
            let errorDetail = 'File not found';
            
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorDetail;
            } catch (e) {
                // JSONでない場合はテキストをそのまま使用
                errorDetail = errorText || errorDetail;
            }
            
            return res.status(404).json({ 
                error: 'File not found',
                detail: errorDetail 
            });
        }

        // レスポンスヘッダーをコピー
        const contentType = response.headers.get('content-type');
        const contentDisposition = response.headers.get('content-disposition');
        
        if (contentType) {
            res.setHeader('Content-Type', contentType);
        }
        if (contentDisposition) {
            res.setHeader('Content-Disposition', contentDisposition);
        }

        // ファイルデータをストリーミング
        const buffer = await response.arrayBuffer();
        res.send(Buffer.from(buffer));

    } catch (error) {
        console.error('Download API error:', error);
        res.status(500).json({ error: 'Internal server error' });
    }
} 