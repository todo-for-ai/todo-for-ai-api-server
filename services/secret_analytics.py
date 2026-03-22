"""
Secret 使用分析服务

分析 Secret 使用趋势、异常检测、生成报告
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import Counter
from sqlalchemy import func, desc
from models import db, AgentSecret, SecretAuditLog

logger = logging.getLogger(__name__)


class SecretUsageAnalyzer:
    """Secret 使用分析器"""

    def __init__(self):
        pass

    def get_usage_trends(
        self,
        secret_id: int,
        days: int = 30
    ) -> List[Dict]:
        """
        获取 Secret 使用趋势

        Args:
            secret_id: Secret ID
            days: 分析天数

        Returns:
            每日使用统计列表
        """
        start_date = datetime.utcnow() - timedelta(days=days)

        # 查询审计日志中的使用记录
        results = db.session.query(
            func.date(SecretAuditLog.timestamp).label('date'),
            func.count(SecretAuditLog.id).label('count')
        ).filter(
            SecretAuditLog.secret_id == secret_id,
            SecretAuditLog.action == 'used',
            SecretAuditLog.timestamp >= start_date
        ).group_by(
            func.date(SecretAuditLog.timestamp)
        ).order_by(
            func.date(SecretAuditLog.timestamp)
        ).all()

        # 填充缺失日期
        trends = []
        date_map = {r.date.isoformat(): r.count for r in results}

        for i in range(days):
            date = (datetime.utcnow() - timedelta(days=days - i - 1)).date()
            date_str = date.isoformat()
            trends.append({
                'date': date_str,
                'count': date_map.get(date_str, 0),
            })

        return trends

    def detect_anomalies(
        self,
        secret_id: int,
        threshold: float = 2.0
    ) -> List[Dict]:
        """
        检测异常使用

        使用标准差方法检测使用量的异常峰值

        Args:
            secret_id: Secret ID
            threshold: 标准差阈值，超过此值为异常

        Returns:
            异常事件列表
        """
        trends = self.get_usage_trends(secret_id, days=30)
        counts = [t['count'] for t in trends]

        if len(counts) < 7:  # 数据不足
            return []

        # 计算均值和标准差
        mean = sum(counts) / len(counts)
        variance = sum((x - mean) ** 2 for x in counts) / len(counts)
        std_dev = variance ** 0.5

        # 检测异常
        anomalies = []
        for trend in trends:
            if trend['count'] > mean + threshold * std_dev:
                anomalies.append({
                    'date': trend['date'],
                    'count': trend['count'],
                    'expected': round(mean, 2),
                    'deviation': round((trend['count'] - mean) / std_dev, 2),
                    'severity': 'high' if trend['count'] > mean + 3 * std_dev else 'medium',
                })

        return anomalies

    def get_usage_heatmap(
        self,
        secret_id: int,
        days: int = 90
    ) -> Dict:
        """
        生成使用热力图数据

        Args:
            secret_id: Secret ID
            days: 天数

        Returns:
            热力图数据，按星期和小时分组
        """
        start_date = datetime.utcnow() - timedelta(days=days)

        results = SecretAuditLog.query.filter(
            SecretAuditLog.secret_id == secret_id,
            SecretAuditLog.action == 'used',
            SecretAuditLog.timestamp >= start_date
        ).all()

        # 按星期几和小时分组
        heatmap = {}
        for log in results:
            dt = log.timestamp
            weekday = dt.weekday()  # 0-6
            hour = dt.hour  # 0-23
            key = f"{weekday}-{hour}"
            heatmap[key] = heatmap.get(key, 0) + 1

        return {
            'days': days,
            'data': heatmap,
            'max_value': max(heatmap.values()) if heatmap else 0,
            'total': len(results),
        }

    def get_top_callers(
        self,
        secret_id: int,
        limit: int = 10
    ) -> List[Dict]:
        """获取最频繁调用者"""
        results = db.session.query(
            SecretAuditLog.actor_type,
            SecretAuditLog.actor_id,
            SecretAuditLog.actor_name,
            func.count(SecretAuditLog.id).label('count')
        ).filter(
            SecretAuditLog.secret_id == secret_id,
            SecretAuditLog.action == 'used'
        ).group_by(
            SecretAuditLog.actor_type,
            SecretAuditLog.actor_id,
            SecretAuditLog.actor_name
        ).order_by(
            desc('count')
        ).limit(limit).all()

        return [
            {
                'actor_type': r.actor_type,
                'actor_id': r.actor_id,
                'actor_name': r.actor_name,
                'count': r.count,
            }
            for r in results
        ]

    def generate_usage_report(
        self,
        secret_id: int,
        days: int = 30
    ) -> Dict:
        """生成完整使用报告"""
        secret = AgentSecret.query.get(secret_id)
        if not secret:
            return {'error': 'Secret not found'}

        trends = self.get_usage_trends(secret_id, days)
        anomalies = self.detect_anomalies(secret_id)
        heatmap = self.get_usage_heatmap(secret_id, days)
        top_callers = self.get_top_callers(secret_id)

        # 计算统计信息
        total_usage = sum(t['count'] for t in trends)
        avg_daily = total_usage / days if days > 0 else 0

        return {
            'secret_id': secret_id,
            'secret_name': secret.name,
            'report_period_days': days,
            'generated_at': datetime.utcnow().isoformat(),
            'summary': {
                'total_usage': total_usage,
                'average_daily': round(avg_daily, 2),
                'anomaly_count': len(anomalies),
                'unique_callers': len(top_callers),
            },
            'trends': trends,
            'anomalies': anomalies,
            'heatmap': heatmap,
            'top_callers': top_callers,
        }

    def get_workspace_secret_stats(self, workspace_id: int) -> Dict:
        """获取工作区 Secret 统计"""
        total_secrets = AgentSecret.query.filter_by(
            workspace_id=workspace_id
        ).count()

        active_secrets = AgentSecret.query.filter_by(
            workspace_id=workspace_id,
            is_active=True
        ).count()

        # 总使用次数
        total_usage = db.session.query(
            func.sum(AgentSecret.usage_count)
        ).filter_by(
            workspace_id=workspace_id
        ).scalar() or 0

        # 高频使用 Secret Top 10
        top_secrets = AgentSecret.query.filter_by(
            workspace_id=workspace_id
        ).order_by(
            desc(AgentSecret.usage_count)
        ).limit(10).all()

        return {
            'total_secrets': total_secrets,
            'active_secrets': active_secrets,
            'revoked_secrets': total_secrets - active_secrets,
            'total_usage': int(total_usage),
            'top_secrets': [
                {
                    'id': s.id,
                    'name': s.name,
                    'usage_count': s.usage_count,
                }
                for s in top_secrets
            ],
        }


# 全局分析器实例
_analyzer: Optional[SecretUsageAnalyzer] = None


def get_secret_analyzer() -> SecretUsageAnalyzer:
    """获取分析器单例"""
    global _analyzer
    if _analyzer is None:
        _analyzer = SecretUsageAnalyzer()
    return _analyzer
