export default async function handler(req, res) {
    if (req.method !== 'GET') {
        return res.status(405).json({ error: 'Method not allowed' });
    }

    const { id } = req.query;

    if (!id) {
        return res.status(400).json({ error: 'Share ID is required' });
    }

    try {
        // バックエンドAPIを呼び出してファイル情報を取得
        const backendUrl = process.env.NEXT_PUBLIC_BACKEND_URL || 'http://localhost:8001';
        const response = await fetch(`${backendUrl}/share/${id}`, {
            method: 'GET',
            headers: {
                'Content-Type': 'application/json',
            },
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

        // HTMLレスポンスからファイル情報を抽出
        const html = await response.text();
        
        // ファイル名を抽出（HTMLから）
        const filenameMatch = html.match(/<span class="info-value">([^<]+)<\/span>/);
        const filename = filenameMatch ? filenameMatch[1] : 'Unknown file';

        // ファイル情報を返す
        res.status(200).json({
            filename: filename,
            share_token: id,
            exists: true
        });

    } catch (error) {
        console.error('Share API error:', error);
        res.status(500).json({ error: 'Internal server error' });
    }
} 